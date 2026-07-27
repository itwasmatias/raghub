from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports.personal.models import (
    CanonicalEvent,
    CompleteBookMarket,
    FeedHealth,
    ModelMetadata,
    MoneylineForecast,
    NormalizedMoneylineQuote,
    QualificationResult,
    ResolvedPredictionRecord,
)
from sports.personal.execution import (
    ExecutionOrder,
    ExecutionReceipt,
    ExecutionTransition,
    ExecutionMode,
    OrderState,
    ReceiptType,
    FailureCode,
)
from sports.personal.wagering import (
    ActorType,
    AggregateType,
    BankrollAccount,
    Bet,
    BetLeg,
    DataQualityStatus,
    DomainEvent,
    Event,
    ExposureSnapshot,
    LedgerEntry,
    LedgerTransaction,
    LedgerTransactionType,
    Market,
    MarketTargetType,
    MarketStatus,
    ModelForecast,
    OrderIntent,
    ProbabilitySnapshot,
    OrderValidationResult,
    Outcome,
    OutcomeResult,
    OutcomeStatus,
    Parlay,
    ParlayStatus,
    Position,
    PositionMode,
    PositionStatus,
    Quote,
    Settlement,
    SettlementRecord,
    SourceType,
    SyntheticPositionValuation,
    WagerKind,
    WagerLeg,
    WagerStatus,
    WagerTicket,
)


class PersonalEditionRepository:
    MIGRATIONS = (
        (
            1,
            """
            CREATE TABLE IF NOT EXISTS sip_events (
                canonical_id TEXT PRIMARY KEY,
                league TEXT NOT NULL,
                start_time TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sip_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                market TEXT NOT NULL,
                period TEXT NOT NULL,
                selection TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (
                    canonical_event_id, sportsbook, market, period,
                    selection, observed_at
                )
            );
            CREATE INDEX IF NOT EXISTS idx_sip_quotes_event_time
                ON sip_quotes(canonical_event_id, observed_at);
            CREATE TABLE IF NOT EXISTS sip_forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                market TEXT NOT NULL,
                period TEXT NOT NULL,
                selection TEXT NOT NULL,
                model_version TEXT NOT NULL,
                forecast_timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (
                    canonical_event_id, market, period, selection,
                    model_version, forecast_timestamp
                )
            );
            CREATE TABLE IF NOT EXISTS sip_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                selection TEXT NOT NULL,
                status TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                data_mode TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sip_evaluations_latest
                ON sip_evaluations(canonical_event_id, evaluated_at DESC);
            CREATE TABLE IF NOT EXISTS sip_feed_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sip_job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                error TEXT
            );
            """,
        ),
        (
            2,
            """
            CREATE TABLE IF NOT EXISTS sip_resolved_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                selection TEXT NOT NULL,
                model_version TEXT NOT NULL,
                forecast_timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (
                    canonical_event_id, selection, model_version,
                    forecast_timestamp
                )
            );
            CREATE INDEX IF NOT EXISTS idx_sip_resolved_predictions_event
                ON sip_resolved_predictions(canonical_event_id, forecast_timestamp);
            """,
        ),
        (
            3,
            """
            CREATE TABLE IF NOT EXISTS sip_bankroll_accounts (
                account_id TEXT PRIMARY KEY,
                currency TEXT NOT NULL,
                balance_usd TEXT NOT NULL,
                available_usd TEXT NOT NULL,
                held_usd TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sip_ledger_entries (
                entry_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                amount_usd TEXT NOT NULL,
                balance_after_usd TEXT NOT NULL,
                available_after_usd TEXT NOT NULL,
                held_after_usd TEXT NOT NULL,
                reference_kind TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_ledger_account_time
                ON sip_ledger_entries(account_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS sip_wagers (
                wager_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                status TEXT NOT NULL,
                wager_kind TEXT NOT NULL,
                stake_usd TEXT NOT NULL,
                potential_profit_usd TEXT NOT NULL,
                potential_payout_usd TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                placed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_wagers_account_time
                ON sip_wagers(account_id, placed_at DESC);
            CREATE TABLE IF NOT EXISTS sip_wager_legs (
                leg_id TEXT PRIMARY KEY,
                wager_id TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                canonical_event_id TEXT NOT NULL,
                market TEXT NOT NULL,
                period TEXT NOT NULL,
                selection TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                american_odds INTEGER NOT NULL,
                model_probability TEXT,
                market_probability TEXT,
                correlation_group TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(wager_id) REFERENCES sip_wagers(wager_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_wager_legs_wager
                ON sip_wager_legs(wager_id, sequence_no);
            CREATE TABLE IF NOT EXISTS sip_settlements (
                settlement_id TEXT PRIMARY KEY,
                wager_id TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL,
                payout_usd TEXT NOT NULL,
                profit_usd TEXT NOT NULL,
                settled_at TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(wager_id) REFERENCES sip_wagers(wager_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_settlements_time
                ON sip_settlements(settled_at DESC);
            CREATE TABLE IF NOT EXISTS sip_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sip_audit_entity
                ON sip_audit_log(entity_kind, entity_id, created_at DESC);
            """,
        ),
        (
            4,
            """
            CREATE TABLE IF NOT EXISTS sip_markets (
                id TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL,
                league TEXT NOT NULL,
                market_type TEXT NOT NULL,
                period TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                opens_at TEXT NOT NULL,
                closes_at TEXT NOT NULL,
                settlement_rule TEXT NOT NULL,
                resolution_source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sip_markets_event
                ON sip_markets(canonical_event_id, status, closes_at);

            CREATE TABLE IF NOT EXISTS sip_outcomes (
                id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                name TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                team_id TEXT,
                status TEXT NOT NULL,
                result TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(market_id, selection_key),
                FOREIGN KEY(market_id) REFERENCES sip_markets(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_outcomes_market
                ON sip_outcomes(market_id);

            CREATE TABLE IF NOT EXISTS sip_market_quotes (
                id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                american_odds INTEGER NOT NULL,
                decimal_odds TEXT NOT NULL,
                implied_probability TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                is_best_price INTEGER NOT NULL,
                is_stale INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_market_quotes_outcome_time
                ON sip_market_quotes(outcome_id, observed_at DESC);

            CREATE TABLE IF NOT EXISTS sip_order_intents (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                stake TEXT NOT NULL,
                requested_odds INTEGER NOT NULL,
                accepted_odds INTEGER NOT NULL,
                mode TEXT NOT NULL,
                estimated_payout TEXT NOT NULL,
                exposure_impact_json TEXT NOT NULL,
                validation_result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id),
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_order_intents_account_time
                ON sip_order_intents(account_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS sip_bets_v2 (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                stake TEXT NOT NULL,
                accepted_odds INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id),
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_bets_v2_account_time
                ON sip_bets_v2(account_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS sip_bet_legs (
                id TEXT PRIMARY KEY,
                bet_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                accepted_odds INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(bet_id) REFERENCES sip_bets_v2(id),
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_bet_legs_bet
                ON sip_bet_legs(bet_id, sequence_no);

            CREATE TABLE IF NOT EXISTS sip_parlays (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                stake TEXT NOT NULL,
                combined_decimal_odds TEXT NOT NULL,
                naive_implied_probability TEXT NOT NULL,
                estimated_sip_probability TEXT,
                potential_return TEXT NOT NULL,
                expected_value TEXT,
                correlation_warning TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_parlays_account_time
                ON sip_parlays(account_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS sip_positions (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                bet_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                stake TEXT NOT NULL,
                average_accepted_odds INTEGER NOT NULL,
                entry_model_probability TEXT,
                current_model_probability TEXT,
                entry_market_probability TEXT,
                current_market_probability TEXT,
                potential_profit TEXT NOT NULL,
                potential_return TEXT NOT NULL,
                estimated_current_value TEXT,
                realized_profit_loss TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                settled_at TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id),
                FOREIGN KEY(bet_id) REFERENCES sip_bets_v2(id),
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_positions_account_status
                ON sip_positions(account_id, status, opened_at DESC);

            CREATE TABLE IF NOT EXISTS sip_ledger_transactions (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                position_id TEXT,
                transaction_type TEXT NOT NULL,
                amount TEXT NOT NULL,
                balance_after TEXT NOT NULL,
                reserved_after TEXT NOT NULL,
                available_after TEXT NOT NULL,
                note TEXT NOT NULL,
                reference_id TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id),
                FOREIGN KEY(position_id) REFERENCES sip_positions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_ledger_tx_account_time
                ON sip_ledger_transactions(account_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS sip_settlements_v2 (
                id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                result TEXT NOT NULL,
                payout TEXT NOT NULL,
                realized_profit_loss TEXT NOT NULL,
                settled_at TEXT NOT NULL,
                resolution_source TEXT NOT NULL,
                notes TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(position_id) REFERENCES sip_positions(id),
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_settlements_v2_time
                ON sip_settlements_v2(settled_at DESC);
            """,
        ),
        (
            5,
            """
            CREATE TABLE IF NOT EXISTS sip_event_catalog (
                id TEXT PRIMARY KEY,
                sport TEXT NOT NULL,
                league TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sip_event_catalog_league_start
                ON sip_event_catalog(league, starts_at, status);

            ALTER TABLE sip_markets ADD COLUMN sport TEXT;
            ALTER TABLE sip_markets ADD COLUMN target_type TEXT;
            ALTER TABLE sip_markets ADD COLUMN provider TEXT;
            ALTER TABLE sip_markets ADD COLUMN home_team TEXT;
            ALTER TABLE sip_markets ADD COLUMN away_team TEXT;
            ALTER TABLE sip_markets ADD COLUMN player_id TEXT;
            ALTER TABLE sip_markets ADD COLUMN player_name TEXT;
            ALTER TABLE sip_markets ADD COLUMN player_team_id TEXT;
            ALTER TABLE sip_markets ADD COLUMN prop_statistic TEXT;
            ALTER TABLE sip_markets ADD COLUMN prop_threshold TEXT;
            ALTER TABLE sip_markets ADD COLUMN metadata_json TEXT;

            ALTER TABLE sip_outcomes ADD COLUMN player_id TEXT;
            ALTER TABLE sip_outcomes ADD COLUMN player_name TEXT;
            ALTER TABLE sip_outcomes ADD COLUMN statistic TEXT;
            ALTER TABLE sip_outcomes ADD COLUMN threshold TEXT;
            ALTER TABLE sip_outcomes ADD COLUMN direction TEXT;

            ALTER TABLE sip_market_quotes ADD COLUMN quote_status TEXT;

            CREATE TABLE IF NOT EXISTS sip_probability_snapshots (
                id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                american_odds INTEGER NOT NULL,
                decimal_odds TEXT NOT NULL,
                raw_implied_probability TEXT NOT NULL,
                no_vig_probability TEXT NOT NULL,
                sportsbook_consensus_probability TEXT NOT NULL,
                sip_adjusted_probability TEXT NOT NULL,
                edge TEXT NOT NULL,
                expected_value TEXT NOT NULL,
                quote_timestamp TEXT NOT NULL,
                forecast_timestamp TEXT NOT NULL,
                model_version TEXT NOT NULL,
                data_quality_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_probability_snapshots_outcome_time
                ON sip_probability_snapshots(outcome_id, quote_timestamp DESC);

            CREATE TABLE IF NOT EXISTS sip_model_forecasts_v2 (
                id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                probability TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                model_version TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                data_quality_status TEXT NOT NULL,
                notes_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(market_id) REFERENCES sip_markets(id),
                FOREIGN KEY(outcome_id) REFERENCES sip_outcomes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_model_forecasts_v2_outcome_time
                ON sip_model_forecasts_v2(outcome_id, generated_at DESC);

            ALTER TABLE sip_positions ADD COLUMN edge_at_entry TEXT;
            ALTER TABLE sip_positions ADD COLUMN current_edge TEXT;
            ALTER TABLE sip_positions ADD COLUMN entry_odds INTEGER;
            ALTER TABLE sip_positions ADD COLUMN current_odds INTEGER;
            ALTER TABLE sip_positions ADD COLUMN market_type TEXT;
            ALTER TABLE sip_positions ADD COLUMN league TEXT;
            ALTER TABLE sip_positions ADD COLUMN event_date TEXT;
            ALTER TABLE sip_positions ADD COLUMN settlement_horizon TEXT;

            CREATE TABLE IF NOT EXISTS sip_synthetic_position_valuations (
                id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                entry_probability TEXT NOT NULL,
                synthetic_shares TEXT NOT NULL,
                current_market_probability TEXT NOT NULL,
                current_sip_probability TEXT NOT NULL,
                estimated_market_value TEXT NOT NULL,
                estimated_sip_value TEXT NOT NULL,
                estimated_change_since_entry_market TEXT NOT NULL,
                estimated_change_since_entry_sip TEXT NOT NULL,
                valuation_timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(position_id) REFERENCES sip_positions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_synthetic_position_valuations_position_time
                ON sip_synthetic_position_valuations(position_id, valuation_timestamp DESC);

            CREATE TABLE IF NOT EXISTS sip_exposure_snapshots (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                as_of TEXT NOT NULL,
                exposure_by_league_json TEXT NOT NULL,
                exposure_by_team_json TEXT NOT NULL,
                exposure_by_player_json TEXT NOT NULL,
                exposure_by_event_json TEXT NOT NULL,
                exposure_by_market_json TEXT NOT NULL,
                exposure_by_market_type_json TEXT NOT NULL,
                exposure_by_sportsbook_json TEXT NOT NULL,
                exposure_by_outcome_json TEXT NOT NULL,
                exposure_by_date_json TEXT NOT NULL,
                exposure_by_settlement_horizon_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_exposure_snapshots_account_time
                ON sip_exposure_snapshots(account_id, as_of DESC);

            CREATE TABLE IF NOT EXISTS sip_domain_events (
                id TEXT PRIMARY KEY,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_version INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                model_version TEXT,
                schema_version TEXT NOT NULL,
                UNIQUE(aggregate_type, aggregate_id, event_version)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_domain_events_aggregate_time
                ON sip_domain_events(aggregate_type, aggregate_id, event_version DESC);
            CREATE INDEX IF NOT EXISTS idx_sip_domain_events_recorded
                ON sip_domain_events(recorded_at DESC);

            CREATE TABLE IF NOT EXISTS sip_aggregate_versions (
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                current_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(aggregate_type, aggregate_id)
            );
            """,
        ),
        (
            6,
            """
            CREATE TABLE IF NOT EXISTS sip_execution_orders (
                id TEXT PRIMARY KEY,
                order_intent_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                stake TEXT NOT NULL,
                requested_odds INTEGER NOT NULL,
                accepted_odds INTEGER NOT NULL,
                sportsbook TEXT NOT NULL,
                mode TEXT NOT NULL,
                state TEXT NOT NULL,
                requires_confirmation INTEGER NOT NULL,
                provider_reference TEXT,
                prefilled_url TEXT,
                failure_code TEXT,
                failure_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(order_intent_id) REFERENCES sip_order_intents(id),
                FOREIGN KEY(account_id) REFERENCES sip_bankroll_accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_execution_orders_account_time
                ON sip_execution_orders(account_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sip_execution_orders_state
                ON sip_execution_orders(state, updated_at DESC);

            CREATE TABLE IF NOT EXISTS sip_execution_state_transitions (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES sip_execution_orders(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_execution_transitions_order
                ON sip_execution_state_transitions(order_id, occurred_at DESC);

            CREATE TABLE IF NOT EXISTS sip_execution_receipts (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                receipt_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES sip_execution_orders(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sip_execution_receipts_order
                ON sip_execution_receipts(order_id, created_at DESC);
            """,
        ),
        (
            7,
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS sip_quotes_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                market TEXT NOT NULL,
                period TEXT NOT NULL,
                selection TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                provider_quote_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(provider_quote_id)
            );

            INSERT INTO sip_quotes_v2 (
                id, canonical_event_id, sportsbook, market, period,
                selection, observed_at, provider_quote_id, payload_json
            )
            WITH legacy_rows AS (
                SELECT
                    id,
                    canonical_event_id,
                    sportsbook,
                    market,
                    period,
                    selection,
                    observed_at,
                    payload_json,
                    COALESCE(
                        NULLIF(json_extract(payload_json, '$.provider_quote_id'), ''),
                        'legacy:' || CAST(id AS TEXT)
                    ) AS base_provider_quote_id
                FROM sip_quotes
            ),
            ranked_rows AS (
                SELECT
                    id,
                    canonical_event_id,
                    sportsbook,
                    market,
                    period,
                    selection,
                    observed_at,
                    payload_json,
                    base_provider_quote_id,
                    COUNT(*) OVER (
                        PARTITION BY base_provider_quote_id
                    ) AS duplicate_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY base_provider_quote_id
                        ORDER BY id
                    ) AS duplicate_index
                FROM legacy_rows
            )
            SELECT
                id,
                canonical_event_id,
                sportsbook,
                market,
                period,
                selection,
                observed_at,
                CASE
                    WHEN duplicate_count = 1 THEN base_provider_quote_id
                    ELSE base_provider_quote_id || ':dup:' || printf('%06d', duplicate_index)
                END,
                payload_json
            FROM ranked_rows
            ORDER BY id;

            DROP TABLE sip_quotes;
            ALTER TABLE sip_quotes_v2 RENAME TO sip_quotes;

            CREATE INDEX IF NOT EXISTS idx_sip_quotes_event_time
                ON sip_quotes(canonical_event_id, observed_at);
            CREATE INDEX IF NOT EXISTS idx_sip_quotes_provider_quote_id
                ON sip_quotes(provider_quote_id);

            COMMIT;
            """,
        ),
    )

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def migrate(self) -> list[int]:
        applied = []
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sip_schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                existing = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM sip_schema_migrations"
                    )
                }
                for version, sql in self.MIGRATIONS:
                    if version in existing:
                        continue
                    connection.executescript(sql)
                    connection.execute(
                        "INSERT INTO sip_schema_migrations(version) VALUES (?)",
                        (version,),
                    )
                    applied.append(version)
        return applied

    def save_market_snapshot(
        self,
        *,
        events: tuple[CanonicalEvent, ...],
        quotes: tuple[NormalizedMoneylineQuote, ...],
        retrieved_at: str,
    ) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                for event in events:
                    connection.execute(
                        """
                        INSERT INTO sip_events VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(canonical_id) DO UPDATE SET
                            league=excluded.league,
                            start_time=excluded.start_time,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            event.canonical_id,
                            event.league,
                            event.start_time,
                            json.dumps(asdict(event), sort_keys=True),
                            retrieved_at,
                        ),
                    )
                connection.executemany(
                    """
                    INSERT INTO sip_quotes (
                        canonical_event_id, sportsbook, market, period,
                        selection, observed_at, provider_quote_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider_quote_id) DO UPDATE SET
                        canonical_event_id=excluded.canonical_event_id,
                        sportsbook=excluded.sportsbook,
                        market=excluded.market,
                        period=excluded.period,
                        selection=excluded.selection,
                        observed_at=excluded.observed_at,
                        payload_json=excluded.payload_json
                    """,
                    [
                        (
                            quote.canonical_event_id,
                            quote.sportsbook,
                            quote.market,
                            quote.period,
                            quote.selection,
                            quote.observed_at,
                            self._quote_observation_id(quote),
                            json.dumps(asdict(quote), sort_keys=True),
                        )
                        for quote in quotes
                    ],
                )

    @staticmethod
    def _quote_observation_id(quote: NormalizedMoneylineQuote) -> str:
        if quote.provider_quote_id:
            return quote.provider_quote_id
        payload = "|".join(
            [
                quote.source.strip().lower(),
                quote.provider_event_id.strip().lower(),
                quote.source_url.strip(),
                quote.observed_at.strip(),
                quote.sportsbook,
                quote.selection,
                str(quote.american_price),
            ]
        )
        return (
            f"quote:legacy:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"
        )

    def save_forecasts(
        self, forecasts: tuple[MoneylineForecast, ...] | list[MoneylineForecast]
    ) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO sip_forecasts (
                        canonical_event_id, market, period, selection,
                        model_version, forecast_timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.canonical_event_id,
                            item.market,
                            item.period,
                            item.selection,
                            item.model_version,
                            item.forecast_timestamp,
                            json.dumps(asdict(item), sort_keys=True),
                        )
                        for item in forecasts
                    ],
                )

        for item in forecasts:
            self.record_domain_event(
                aggregate_type=AggregateType.FORECAST,
                aggregate_id=(
                    f"{item.canonical_event_id}:{item.market}:{item.period}:{item.selection}"
                ),
                event_type="model_forecast_created",
                actor_type=ActorType.MODEL,
                actor_id="sip-model",
                payload={
                    "canonical_event_id": item.canonical_event_id,
                    "market": item.market,
                    "period": item.period,
                    "selection": item.selection,
                    "raw_probability": str(item.raw_probability),
                    "calibrated_probability": str(item.calibrated_probability),
                    "calibration_status": item.calibration_status,
                },
                source=SourceType.MODEL_PIPELINE,
                model_version=item.model_version,
                occurred_at=item.forecast_timestamp,
            )

    def save_evaluation(self, result: QualificationResult, *, data_mode: str) -> int:
        self.migrate()
        payload = asdict(result)
        payload["reason_codes"] = [
            getattr(item, "value", str(item)) for item in result.reason_codes
        ]
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO sip_evaluations (
                        canonical_event_id, selection, status,
                        evaluated_at, data_mode, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.canonical_event_id,
                        result.selection,
                        result.status,
                        result.evaluated_at,
                        data_mode,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                return int(cursor.lastrowid)

    def save_feed_health(self, health: FeedHealth) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_feed_state VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json
                    """,
                    (json.dumps(asdict(health), sort_keys=True),),
                )

    def load_feed_health(self) -> FeedHealth | None:
        self.migrate()
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM sip_feed_state WHERE id = 1"
            ).fetchone()
        return FeedHealth(**json.loads(row[0])) if row else None

    def list_events(self) -> list[CanonicalEvent]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM sip_events ORDER BY start_time"
            ).fetchall()
        return [
            CanonicalEvent(
                **{
                    **json.loads(row[0]),
                    "provider_event_ids": tuple(
                        json.loads(row[0])["provider_event_ids"]
                    ),
                    "source_urls": tuple(json.loads(row[0])["source_urls"]),
                }
            )
            for row in rows
        ]

    def list_quotes(self) -> list[NormalizedMoneylineQuote]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM sip_quotes ORDER BY observed_at"
            ).fetchall()
        return [NormalizedMoneylineQuote(**json.loads(row[0])) for row in rows]

    def list_forecasts(self) -> list[MoneylineForecast]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM sip_forecasts ORDER BY forecast_timestamp"
            ).fetchall()
        values = []
        for (payload_text,) in rows:
            payload = json.loads(payload_text)
            metadata = payload["metadata"]
            metadata["training_period"] = tuple(metadata["training_period"])
            metadata["validation_period"] = tuple(metadata["validation_period"])
            payload["metadata"] = ModelMetadata(**metadata)
            payload["contributing_factors"] = tuple(payload["contributing_factors"])
            payload["missing_feature_warnings"] = tuple(
                payload["missing_feature_warnings"]
            )
            values.append(MoneylineForecast(**payload))
        return values

    def latest_evaluations(self) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM sip_evaluations
                WHERE id IN (
                    SELECT MAX(id) FROM sip_evaluations
                    GROUP BY canonical_event_id, selection
                )
                ORDER BY evaluated_at DESC
                """
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def start_job(self, name: str, started_at: str) -> int:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO sip_job_runs (
                        job_name, started_at, status, attempts
                    ) VALUES (?, ?, 'running', 1)
                    """,
                    (name, started_at),
                )
                return int(cursor.lastrowid)

    def finish_job(
        self,
        job_id: int,
        *,
        finished_at: str,
        status: str,
        attempts: int,
        error: str | None,
    ) -> None:
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE sip_job_runs
                    SET finished_at=?, status=?, attempts=?, error=?
                    WHERE id=?
                    """,
                    (finished_at, status, attempts, error, job_id),
                )

    def recent_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM sip_job_runs
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_resolved_predictions(
        self,
        rows: tuple[ResolvedPredictionRecord, ...] | list[ResolvedPredictionRecord],
    ) -> None:
        if not rows:
            return
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO sip_resolved_predictions (
                        canonical_event_id, selection, model_version,
                        forecast_timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.canonical_event_id,
                            item.selection,
                            item.model_version,
                            item.forecast_timestamp,
                            json.dumps(asdict(item), sort_keys=True),
                        )
                        for item in rows
                    ],
                )

    def list_resolved_predictions(
        self,
    ) -> list[ResolvedPredictionRecord]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_resolved_predictions
                ORDER BY forecast_timestamp
                """
            ).fetchall()
        return [ResolvedPredictionRecord(**json.loads(row[0])) for row in rows]

    def resolved_prediction_keys(self) -> set[tuple[str, str, str, str]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT canonical_event_id, selection, model_version, forecast_timestamp
                FROM sip_resolved_predictions
                """
            ).fetchall()
        return {(str(a), str(b), str(c), str(d)) for a, b, c, d in rows}

    def save_bankroll_account(self, account: BankrollAccount) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_bankroll_accounts (
                        account_id, currency, balance_usd, available_usd,
                        held_usd, status, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        currency=excluded.currency,
                        balance_usd=excluded.balance_usd,
                        available_usd=excluded.available_usd,
                        held_usd=excluded.held_usd,
                        status=excluded.status,
                        metadata_json=excluded.metadata_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        account.account_id,
                        account.currency,
                        str(account.balance_usd),
                        str(account.available_usd),
                        str(account.held_usd),
                        account.status,
                        json.dumps(account.metadata, sort_keys=True),
                        account.created_at,
                        account.updated_at,
                    ),
                )
                self.record_audit_event(
                    entity_kind="bankroll_account",
                    entity_id=account.account_id,
                    action="upsert",
                    payload={
                        "status": account.status,
                        "balance_usd": str(account.balance_usd),
                        "available_usd": str(account.available_usd),
                        "held_usd": str(account.held_usd),
                    },
                    connection=connection,
                )

    def get_bankroll_account(self, account_id: str) -> BankrollAccount | None:
        self.migrate()
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT account_id, currency, balance_usd, available_usd,
                    held_usd, status, metadata_json, created_at, updated_at
                FROM sip_bankroll_accounts
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return BankrollAccount(
            account_id=str(row[0]),
            currency=str(row[1]),
            balance_usd=Decimal(str(row[2])),
            available_usd=Decimal(str(row[3])),
            held_usd=Decimal(str(row[4])),
            status=str(row[5]),
            metadata=json.loads(row[6]),
            created_at=str(row[7]),
            updated_at=str(row[8]),
        )

    def append_ledger_entry(self, entry: LedgerEntry) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO sip_ledger_entries (
                            entry_id, account_id, entry_type, amount_usd,
                            balance_after_usd, available_after_usd, held_after_usd,
                            reference_kind, reference_id, idempotency_key,
                            created_at, note, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry.entry_id,
                            entry.account_id,
                            entry.entry_type.value,
                            str(entry.amount_usd),
                            str(entry.balance_after_usd),
                            str(entry.available_after_usd),
                            str(entry.held_after_usd),
                            entry.reference_kind,
                            entry.reference_id,
                            entry.idempotency_key,
                            entry.created_at,
                            entry.note,
                            json.dumps(
                                {
                                    "entry_id": entry.entry_id,
                                    "account_id": entry.account_id,
                                    "entry_type": entry.entry_type.value,
                                    "amount_usd": str(entry.amount_usd),
                                    "balance_after_usd": str(entry.balance_after_usd),
                                    "available_after_usd": str(
                                        entry.available_after_usd
                                    ),
                                    "held_after_usd": str(entry.held_after_usd),
                                    "reference_kind": entry.reference_kind,
                                    "reference_id": entry.reference_id,
                                    "idempotency_key": entry.idempotency_key,
                                    "created_at": entry.created_at,
                                    "note": entry.note,
                                },
                                sort_keys=True,
                            ),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    if "idempotency_key" in str(error):
                        raise ValueError(
                            f"duplicate ledger idempotency key: {entry.idempotency_key}"
                        ) from error
                    raise

                connection.execute(
                    """
                    UPDATE sip_bankroll_accounts
                    SET balance_usd=?, available_usd=?, held_usd=?, updated_at=?
                    WHERE account_id=?
                    """,
                    (
                        str(entry.balance_after_usd),
                        str(entry.available_after_usd),
                        str(entry.held_after_usd),
                        entry.created_at,
                        entry.account_id,
                    ),
                )
                self.record_audit_event(
                    entity_kind="ledger_entry",
                    entity_id=entry.entry_id,
                    action="append",
                    payload={
                        "account_id": entry.account_id,
                        "entry_type": entry.entry_type.value,
                        "amount_usd": str(entry.amount_usd),
                    },
                    connection=connection,
                )

    def list_ledger_entries(
        self, account_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_ledger_entries
                WHERE account_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (account_id, max(1, limit)),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_wager_ticket(self, ticket: WagerTicket) -> str:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                existing = connection.execute(
                    "SELECT wager_id FROM sip_wagers WHERE idempotency_key = ?",
                    (ticket.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return str(existing[0])

                payload = {
                    "wager_id": ticket.wager_id,
                    "account_id": ticket.account_id,
                    "kind": ticket.kind.value,
                    "status": ticket.status.value,
                    "stake_usd": str(ticket.stake_usd),
                    "potential_profit_usd": str(ticket.potential_profit_usd),
                    "potential_payout_usd": str(ticket.potential_payout_usd),
                    "execution_mode": ticket.execution_mode,
                    "idempotency_key": ticket.idempotency_key,
                    "placed_at": ticket.placed_at,
                    "updated_at": ticket.updated_at,
                    "validation_notes": list(ticket.validation_notes),
                    "metadata": dict(ticket.metadata),
                }
                connection.execute(
                    """
                    INSERT INTO sip_wagers (
                        wager_id, account_id, status, wager_kind,
                        stake_usd, potential_profit_usd, potential_payout_usd,
                        execution_mode, idempotency_key, placed_at, updated_at,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket.wager_id,
                        ticket.account_id,
                        ticket.status.value,
                        ticket.kind.value,
                        str(ticket.stake_usd),
                        str(ticket.potential_profit_usd),
                        str(ticket.potential_payout_usd),
                        ticket.execution_mode,
                        ticket.idempotency_key,
                        ticket.placed_at,
                        ticket.updated_at,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO sip_wager_legs (
                        leg_id, wager_id, sequence_no, canonical_event_id,
                        market, period, selection, sportsbook, american_odds,
                        model_probability, market_probability, correlation_group,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            leg.leg_id,
                            ticket.wager_id,
                            index,
                            leg.canonical_event_id,
                            leg.market,
                            leg.period,
                            leg.selection,
                            leg.sportsbook,
                            leg.american_odds,
                            str(leg.model_probability)
                            if leg.model_probability is not None
                            else None,
                            str(leg.market_probability)
                            if leg.market_probability is not None
                            else None,
                            leg.correlation_group,
                            json.dumps(
                                {
                                    "leg_id": leg.leg_id,
                                    "canonical_event_id": leg.canonical_event_id,
                                    "market": leg.market,
                                    "period": leg.period,
                                    "selection": leg.selection,
                                    "sportsbook": leg.sportsbook,
                                    "american_odds": leg.american_odds,
                                    "model_probability": str(leg.model_probability)
                                    if leg.model_probability is not None
                                    else None,
                                    "market_probability": str(leg.market_probability)
                                    if leg.market_probability is not None
                                    else None,
                                    "correlation_group": leg.correlation_group,
                                },
                                sort_keys=True,
                            ),
                        )
                        for index, leg in enumerate(ticket.legs, start=1)
                    ],
                )
                self.record_audit_event(
                    entity_kind="wager",
                    entity_id=ticket.wager_id,
                    action="create",
                    payload={
                        "kind": ticket.kind.value,
                        "status": ticket.status.value,
                        "idempotency_key": ticket.idempotency_key,
                        "legs": len(ticket.legs),
                    },
                    connection=connection,
                )
                return ticket.wager_id

    def get_wager_ticket(self, wager_id: str) -> WagerTicket | None:
        self.migrate()
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT status, payload_json FROM sip_wagers WHERE wager_id = ?",
                (wager_id,),
            ).fetchone()
            if row is None:
                return None
            row_status = str(row[0])
            payload = json.loads(row[1])
            legs_rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_wager_legs
                WHERE wager_id = ?
                ORDER BY sequence_no
                """,
                (wager_id,),
            ).fetchall()

        legs = []
        for (leg_payload,) in legs_rows:
            item = json.loads(leg_payload)
            legs.append(
                WagerLeg(
                    leg_id=str(item["leg_id"]),
                    canonical_event_id=str(item["canonical_event_id"]),
                    market=str(item["market"]),
                    period=str(item["period"]),
                    selection=str(item["selection"]),
                    sportsbook=str(item["sportsbook"]),
                    american_odds=int(item["american_odds"]),
                    model_probability=(
                        Decimal(str(item["model_probability"]))
                        if item.get("model_probability") is not None
                        else None
                    ),
                    market_probability=(
                        Decimal(str(item["market_probability"]))
                        if item.get("market_probability") is not None
                        else None
                    ),
                    correlation_group=item.get("correlation_group"),
                )
            )

        return WagerTicket(
            wager_id=str(payload["wager_id"]),
            account_id=str(payload["account_id"]),
            kind=WagerKind(str(payload["kind"])),
            status=WagerStatus(row_status),
            stake_usd=Decimal(str(payload["stake_usd"])),
            potential_profit_usd=Decimal(str(payload["potential_profit_usd"])),
            potential_payout_usd=Decimal(str(payload["potential_payout_usd"])),
            execution_mode=str(payload["execution_mode"]),
            idempotency_key=str(payload["idempotency_key"]),
            placed_at=str(payload["placed_at"]),
            updated_at=str(payload["updated_at"]),
            legs=tuple(legs),
            validation_notes=tuple(payload.get("validation_notes") or []),
            metadata=dict(payload.get("metadata") or {}),
        )

    def save_settlement(self, settlement: SettlementRecord) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_settlements (
                        settlement_id, wager_id, outcome, payout_usd, profit_usd,
                        settled_at, source, notes, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        settlement.settlement_id,
                        settlement.wager_id,
                        settlement.outcome.value,
                        str(settlement.payout_usd),
                        str(settlement.profit_usd),
                        settlement.settled_at,
                        settlement.source,
                        settlement.notes,
                        json.dumps(
                            {
                                "settlement_id": settlement.settlement_id,
                                "wager_id": settlement.wager_id,
                                "outcome": settlement.outcome.value,
                                "payout_usd": str(settlement.payout_usd),
                                "profit_usd": str(settlement.profit_usd),
                                "settled_at": settlement.settled_at,
                                "source": settlement.source,
                                "notes": settlement.notes,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                connection.execute(
                    "UPDATE sip_wagers SET status=?, updated_at=? WHERE wager_id=?",
                    (
                        WagerStatus.SETTLED.value,
                        settlement.settled_at,
                        settlement.wager_id,
                    ),
                )
                self.record_audit_event(
                    entity_kind="settlement",
                    entity_id=settlement.settlement_id,
                    action="record",
                    payload={
                        "wager_id": settlement.wager_id,
                        "outcome": settlement.outcome.value,
                        "payout_usd": str(settlement.payout_usd),
                        "profit_usd": str(settlement.profit_usd),
                    },
                    connection=connection,
                )

    def list_audit_events(
        self,
        *,
        entity_kind: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT entity_kind, entity_id, action, payload_json, created_at FROM sip_audit_log"
        clauses = []
        params: list[Any] = []
        if entity_kind:
            clauses.append("entity_kind = ?")
            params.append(entity_kind)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            {
                "entity_kind": str(row[0]),
                "entity_id": str(row[1]),
                "action": str(row[2]),
                "payload": json.loads(row[3]),
                "created_at": str(row[4]),
            }
            for row in rows
        ]

    def upsert_market_bundle(
        self,
        *,
        markets: tuple[Market, ...] | list[Market],
        outcomes: tuple[Outcome, ...] | list[Outcome],
        quotes: tuple[Quote, ...] | list[Quote],
    ) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO sip_markets (
                        id, canonical_event_id, league, market_type, period,
                        title, description, status, opens_at, closes_at,
                        settlement_rule, resolution_source, created_at, updated_at,
                        payload_json, sport, target_type, provider, home_team,
                        away_team, player_id, player_name, player_team_id,
                        prop_statistic, prop_threshold, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        canonical_event_id=excluded.canonical_event_id,
                        league=excluded.league,
                        market_type=excluded.market_type,
                        period=excluded.period,
                        title=excluded.title,
                        description=excluded.description,
                        status=excluded.status,
                        opens_at=excluded.opens_at,
                        closes_at=excluded.closes_at,
                        settlement_rule=excluded.settlement_rule,
                        resolution_source=excluded.resolution_source,
                        updated_at=excluded.updated_at,
                        payload_json=excluded.payload_json,
                        sport=excluded.sport,
                        target_type=excluded.target_type,
                        provider=excluded.provider,
                        home_team=excluded.home_team,
                        away_team=excluded.away_team,
                        player_id=excluded.player_id,
                        player_name=excluded.player_name,
                        player_team_id=excluded.player_team_id,
                        prop_statistic=excluded.prop_statistic,
                        prop_threshold=excluded.prop_threshold,
                        metadata_json=excluded.metadata_json
                    """,
                    [
                        (
                            item.id,
                            item.canonical_event_id,
                            item.league,
                            item.market_type,
                            item.period,
                            item.title,
                            item.description,
                            item.status.value,
                            item.opens_at,
                            item.closes_at,
                            item.settlement_rule,
                            item.resolution_source,
                            item.created_at,
                            item.updated_at,
                            json.dumps(
                                {
                                    "id": item.id,
                                    "canonical_event_id": item.canonical_event_id,
                                    "league": item.league,
                                    "market_type": item.market_type,
                                    "period": item.period,
                                    "title": item.title,
                                    "description": item.description,
                                    "status": item.status.value,
                                    "opens_at": item.opens_at,
                                    "closes_at": item.closes_at,
                                    "settlement_rule": item.settlement_rule,
                                    "resolution_source": item.resolution_source,
                                    "created_at": item.created_at,
                                    "updated_at": item.updated_at,
                                    "sport": item.sport,
                                    "target_type": item.target_type.value,
                                    "provider": item.provider,
                                    "home_team": item.home_team,
                                    "away_team": item.away_team,
                                    "player_id": item.player_id,
                                    "player_name": item.player_name,
                                    "player_team_id": item.player_team_id,
                                    "prop_statistic": item.prop_statistic,
                                    "prop_threshold": (
                                        str(item.prop_threshold)
                                        if item.prop_threshold is not None
                                        else None
                                    ),
                                    "metadata": item.metadata,
                                },
                                sort_keys=True,
                            ),
                            item.sport,
                            item.target_type.value,
                            item.provider,
                            item.home_team,
                            item.away_team,
                            item.player_id,
                            item.player_name,
                            item.player_team_id,
                            item.prop_statistic,
                            str(item.prop_threshold)
                            if item.prop_threshold is not None
                            else None,
                            json.dumps(item.metadata, sort_keys=True),
                        )
                        for item in markets
                    ],
                )

                connection.executemany(
                    """
                    INSERT INTO sip_outcomes (
                        id, market_id, name, selection_key, team_id,
                        status, result, payload_json, player_id,
                        player_name, statistic, threshold, direction
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        market_id=excluded.market_id,
                        name=excluded.name,
                        selection_key=excluded.selection_key,
                        team_id=excluded.team_id,
                        status=excluded.status,
                        result=excluded.result,
                        payload_json=excluded.payload_json,
                        player_id=excluded.player_id,
                        player_name=excluded.player_name,
                        statistic=excluded.statistic,
                        threshold=excluded.threshold,
                        direction=excluded.direction
                    """,
                    [
                        (
                            item.id,
                            item.market_id,
                            item.name,
                            item.selection_key,
                            item.team_id,
                            item.status.value,
                            item.result.value,
                            json.dumps(
                                {
                                    "id": item.id,
                                    "market_id": item.market_id,
                                    "name": item.name,
                                    "selection_key": item.selection_key,
                                    "team_id": item.team_id,
                                    "status": item.status.value,
                                    "result": item.result.value,
                                    "player_id": item.player_id,
                                    "player_name": item.player_name,
                                    "statistic": item.statistic,
                                    "threshold": (
                                        str(item.threshold)
                                        if item.threshold is not None
                                        else None
                                    ),
                                    "direction": item.direction,
                                },
                                sort_keys=True,
                            ),
                            item.player_id,
                            item.player_name,
                            item.statistic,
                            str(item.threshold) if item.threshold is not None else None,
                            item.direction,
                        )
                        for item in outcomes
                    ],
                )

                connection.executemany(
                    """
                    INSERT OR REPLACE INTO sip_market_quotes (
                        id, market_id, outcome_id, provider, sportsbook,
                        american_odds, decimal_odds, implied_probability,
                        observed_at, is_best_price, is_stale, payload_json,
                        quote_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.id,
                            item.market_id,
                            item.outcome_id,
                            item.provider,
                            item.sportsbook,
                            item.american_odds,
                            str(item.decimal_odds),
                            str(item.implied_probability),
                            item.observed_at,
                            1 if item.is_best_price else 0,
                            1 if item.is_stale else 0,
                            json.dumps(
                                {
                                    "id": item.id,
                                    "market_id": item.market_id,
                                    "outcome_id": item.outcome_id,
                                    "provider": item.provider,
                                    "sportsbook": item.sportsbook,
                                    "american_odds": item.american_odds,
                                    "decimal_odds": str(item.decimal_odds),
                                    "implied_probability": str(
                                        item.implied_probability
                                    ),
                                    "observed_at": item.observed_at,
                                    "is_best_price": item.is_best_price,
                                    "is_stale": item.is_stale,
                                    "quote_status": item.quote_status.value,
                                },
                                sort_keys=True,
                            ),
                            item.quote_status.value,
                        )
                        for item in quotes
                    ],
                )

    def list_markets(
        self,
        *,
        sport: str | None = None,
        league: str | None = None,
        date: str | None = None,
        team: str | None = None,
        player: str | None = None,
        market_type: str | None = None,
        provider: str | None = None,
        status: MarketStatus | None = None,
        search: str | None = None,
        sort_by: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.migrate()
        where = []
        params: list[Any] = []
        if sport:
            where.append("COALESCE(sport, '') = ?")
            params.append(sport)
        if league:
            where.append("league = ?")
            params.append(league)
        if date:
            where.append("substr(opens_at, 1, 10) = ?")
            params.append(date)
        if team:
            where.append("(COALESCE(home_team, '') = ? OR COALESCE(away_team, '') = ?)")
            params.extend([team, team])
        if player:
            where.append(
                "(COALESCE(player_id, '') LIKE ? OR COALESCE(player_name, '') LIKE ?)"
            )
            like_player = f"%{player.strip()}%"
            params.extend([like_player, like_player])
        if market_type:
            where.append("market_type = ?")
            params.append(market_type)
        if provider:
            where.append("COALESCE(provider, '') = ?")
            params.append(provider)
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        if search:
            where.append(
                "(title LIKE ? OR description LIKE ? OR COALESCE(home_team, '') LIKE ? OR COALESCE(away_team, '') LIKE ? OR COALESCE(player_name, '') LIKE ? OR COALESCE(prop_statistic, '') LIKE ?)"
            )
            like = f"%{search.strip()}%"
            params.extend([like, like, like, like, like, like])
        query = "SELECT payload_json FROM sip_markets"
        if where:
            query += " WHERE " + " AND ".join(where)

        sort_map = {
            "start_time": "opens_at ASC",
            "sip_edge": "COALESCE(json_extract(payload_json, '$.top_edge'), 0) DESC",
            "expected_value": "COALESCE(json_extract(payload_json, '$.top_expected_value'), 0) DESC",
            "popularity": "COALESCE(json_extract(payload_json, '$.popularity_score'), 0) DESC",
            "open_exposure": "COALESCE(json_extract(payload_json, '$.open_exposure'), 0) DESC",
        }
        query += " ORDER BY " + sort_map.get(sort_by or "", "opens_at ASC") + " LIMIT ?"
        params.append(max(1, limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [json.loads(item[0]) for item in rows]

    def upsert_event_catalog(
        self,
        events: tuple[Event, ...] | list[Event],
    ) -> None:
        if not events:
            return
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO sip_event_catalog (
                        id, sport, league, starts_at, home_team, away_team,
                        status, provider, created_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        sport=excluded.sport,
                        league=excluded.league,
                        starts_at=excluded.starts_at,
                        home_team=excluded.home_team,
                        away_team=excluded.away_team,
                        status=excluded.status,
                        provider=excluded.provider,
                        updated_at=excluded.updated_at,
                        payload_json=excluded.payload_json
                    """,
                    [
                        (
                            item.id,
                            item.sport,
                            item.league,
                            item.starts_at,
                            item.home_team,
                            item.away_team,
                            item.status.value,
                            item.provider,
                            item.created_at,
                            item.updated_at,
                            json.dumps(
                                {
                                    "id": item.id,
                                    "sport": item.sport,
                                    "league": item.league,
                                    "starts_at": item.starts_at,
                                    "home_team": item.home_team,
                                    "away_team": item.away_team,
                                    "status": item.status.value,
                                    "provider": item.provider,
                                    "created_at": item.created_at,
                                    "updated_at": item.updated_at,
                                    "metadata": item.metadata,
                                },
                                sort_keys=True,
                            ),
                        )
                        for item in events
                    ],
                )

    def list_event_catalog(
        self,
        *,
        sport: str | None = None,
        league: str | None = None,
        date: str | None = None,
        team: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        self.migrate()
        where = []
        params: list[Any] = []
        if sport:
            where.append("sport = ?")
            params.append(sport)
        if league:
            where.append("league = ?")
            params.append(league)
        if date:
            where.append("substr(starts_at, 1, 10) = ?")
            params.append(date)
        if team:
            where.append("(home_team LIKE ? OR away_team LIKE ?)")
            like_team = f"%{team.strip()}%"
            params.extend([like_team, like_team])
        if status:
            where.append("status = ?")
            params.append(status)
        if search:
            where.append("(home_team LIKE ? OR away_team LIKE ? OR league LIKE ?)")
            like_search = f"%{search.strip()}%"
            params.extend([like_search, like_search, like_search])

        query = "SELECT payload_json FROM sip_event_catalog"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY starts_at ASC LIMIT ?"
        params.append(max(1, limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_market_detail(self, market_id: str) -> dict[str, Any] | None:
        self.migrate()
        with closing(self.connect()) as connection:
            market_row = connection.execute(
                "SELECT payload_json FROM sip_markets WHERE id = ?",
                (market_id,),
            ).fetchone()
            if market_row is None:
                return None
            outcomes_rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_outcomes
                WHERE market_id = ?
                ORDER BY selection_key
                """,
                (market_id,),
            ).fetchall()
            quotes_rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_market_quotes
                WHERE market_id = ?
                ORDER BY observed_at DESC
                """,
                (market_id,),
            ).fetchall()
            snapshots_rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_probability_snapshots
                WHERE market_id = ?
                ORDER BY quote_timestamp DESC
                LIMIT 300
                """,
                (market_id,),
            ).fetchall()
            forecasts_rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_model_forecasts_v2
                WHERE market_id = ?
                ORDER BY generated_at DESC
                LIMIT 300
                """,
                (market_id,),
            ).fetchall()
            positions_rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_positions
                WHERE market_id = ?
                ORDER BY opened_at DESC
                LIMIT 25
                """,
                (market_id,),
            ).fetchall()
        return {
            "market": json.loads(market_row[0]),
            "outcomes": [json.loads(row[0]) for row in outcomes_rows],
            "quotes": [json.loads(row[0]) for row in quotes_rows],
            "probability_snapshots": [json.loads(row[0]) for row in snapshots_rows],
            "model_forecasts": [json.loads(row[0]) for row in forecasts_rows],
            "positions": [json.loads(row[0]) for row in positions_rows],
        }

    def save_probability_snapshots(
        self,
        snapshots: tuple[ProbabilitySnapshot, ...] | list[ProbabilitySnapshot],
    ) -> None:
        if not snapshots:
            return
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO sip_probability_snapshots (
                        id, market_id, outcome_id, provider, sportsbook,
                        american_odds, decimal_odds, raw_implied_probability,
                        no_vig_probability, sportsbook_consensus_probability,
                        sip_adjusted_probability, edge, expected_value,
                        quote_timestamp, forecast_timestamp, model_version,
                        data_quality_status, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.id,
                            item.market_id,
                            item.outcome_id,
                            item.provider,
                            item.sportsbook,
                            item.american_odds,
                            str(item.decimal_odds),
                            str(item.raw_implied_probability),
                            str(item.no_vig_probability),
                            str(item.sportsbook_consensus_probability),
                            str(item.sip_adjusted_probability),
                            str(item.edge),
                            str(item.expected_value),
                            item.quote_timestamp,
                            item.forecast_timestamp,
                            item.model_version,
                            item.data_quality_status.value,
                            json.dumps(
                                {
                                    "id": item.id,
                                    "market_id": item.market_id,
                                    "outcome_id": item.outcome_id,
                                    "provider": item.provider,
                                    "sportsbook": item.sportsbook,
                                    "american_odds": item.american_odds,
                                    "decimal_odds": str(item.decimal_odds),
                                    "raw_implied_probability": str(
                                        item.raw_implied_probability
                                    ),
                                    "no_vig_probability": str(item.no_vig_probability),
                                    "sportsbook_consensus_probability": str(
                                        item.sportsbook_consensus_probability
                                    ),
                                    "sip_adjusted_probability": str(
                                        item.sip_adjusted_probability
                                    ),
                                    "edge": str(item.edge),
                                    "expected_value": str(item.expected_value),
                                    "quote_timestamp": item.quote_timestamp,
                                    "forecast_timestamp": item.forecast_timestamp,
                                    "model_version": item.model_version,
                                    "data_quality_status": item.data_quality_status.value,
                                },
                                sort_keys=True,
                            ),
                        )
                        for item in snapshots
                    ],
                )

    def list_probability_history(
        self,
        *,
        market_id: str,
        outcome_id: str | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT payload_json FROM sip_probability_snapshots WHERE market_id = ?"
        params: list[Any] = [market_id]
        if outcome_id:
            query += " AND outcome_id = ?"
            params.append(outcome_id)
        query += " ORDER BY quote_timestamp DESC LIMIT ?"
        params.append(max(1, limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_model_forecasts_v2(
        self,
        forecasts: tuple[ModelForecast, ...] | list[ModelForecast],
    ) -> None:
        if not forecasts:
            return
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO sip_model_forecasts_v2 (
                        id, market_id, outcome_id, probability,
                        generated_at, model_version, feature_version,
                        data_quality_status, notes_json, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.id,
                            item.market_id,
                            item.outcome_id,
                            str(item.probability),
                            item.generated_at,
                            item.model_version,
                            item.feature_version,
                            item.data_quality_status.value,
                            json.dumps(list(item.notes), sort_keys=True),
                            json.dumps(
                                {
                                    "id": item.id,
                                    "market_id": item.market_id,
                                    "outcome_id": item.outcome_id,
                                    "probability": str(item.probability),
                                    "generated_at": item.generated_at,
                                    "model_version": item.model_version,
                                    "feature_version": item.feature_version,
                                    "data_quality_status": item.data_quality_status.value,
                                    "notes": list(item.notes),
                                },
                                sort_keys=True,
                            ),
                        )
                        for item in forecasts
                    ],
                )

    def save_synthetic_position_valuation(
        self,
        valuation: SyntheticPositionValuation,
    ) -> str:
        self.migrate()
        payload = {
            "id": valuation.id,
            "position_id": valuation.position_id,
            "entry_probability": str(valuation.entry_probability),
            "synthetic_shares": str(valuation.synthetic_shares),
            "current_market_probability": str(valuation.current_market_probability),
            "current_sip_probability": str(valuation.current_sip_probability),
            "estimated_market_value": str(valuation.estimated_market_value),
            "estimated_sip_value": str(valuation.estimated_sip_value),
            "estimated_change_since_entry_market": str(
                valuation.estimated_change_since_entry_market
            ),
            "estimated_change_since_entry_sip": str(
                valuation.estimated_change_since_entry_sip
            ),
            "valuation_timestamp": valuation.valuation_timestamp,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_synthetic_position_valuations (
                        id, position_id, entry_probability, synthetic_shares,
                        current_market_probability, current_sip_probability,
                        estimated_market_value, estimated_sip_value,
                        estimated_change_since_entry_market,
                        estimated_change_since_entry_sip,
                        valuation_timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        valuation.id,
                        valuation.position_id,
                        str(valuation.entry_probability),
                        str(valuation.synthetic_shares),
                        str(valuation.current_market_probability),
                        str(valuation.current_sip_probability),
                        str(valuation.estimated_market_value),
                        str(valuation.estimated_sip_value),
                        str(valuation.estimated_change_since_entry_market),
                        str(valuation.estimated_change_since_entry_sip),
                        valuation.valuation_timestamp,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        self.record_domain_event(
            aggregate_type=AggregateType.POSITION,
            aggregate_id=valuation.position_id,
            event_type="position_update",
            actor_type=ActorType.MODEL,
            actor_id="valuation-engine",
            payload=payload,
            source=SourceType.MODEL_PIPELINE,
            occurred_at=valuation.valuation_timestamp,
        )
        return valuation.id

    def latest_synthetic_valuations(
        self,
        *,
        account_id: str,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT v.payload_json
                FROM sip_synthetic_position_valuations v
                JOIN sip_positions p ON p.id = v.position_id
                WHERE p.account_id = ?
                ORDER BY v.valuation_timestamp DESC
                LIMIT ?
                """,
                (account_id, max(1, limit)),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_exposure_snapshot(self, snapshot: ExposureSnapshot) -> str:
        self.migrate()

        def _dump(values: dict[str, Decimal]) -> str:
            return json.dumps({k: str(v) for k, v in values.items()}, sort_keys=True)

        payload = {
            "id": snapshot.id,
            "account_id": snapshot.account_id,
            "as_of": snapshot.as_of,
            "exposure_by_league": {
                k: str(v) for k, v in snapshot.exposure_by_league.items()
            },
            "exposure_by_team": {
                k: str(v) for k, v in snapshot.exposure_by_team.items()
            },
            "exposure_by_player": {
                k: str(v) for k, v in snapshot.exposure_by_player.items()
            },
            "exposure_by_event": {
                k: str(v) for k, v in snapshot.exposure_by_event.items()
            },
            "exposure_by_market": {
                k: str(v) for k, v in snapshot.exposure_by_market.items()
            },
            "exposure_by_market_type": {
                k: str(v) for k, v in snapshot.exposure_by_market_type.items()
            },
            "exposure_by_sportsbook": {
                k: str(v) for k, v in snapshot.exposure_by_sportsbook.items()
            },
            "exposure_by_outcome": {
                k: str(v) for k, v in snapshot.exposure_by_outcome.items()
            },
            "exposure_by_date": {
                k: str(v) for k, v in snapshot.exposure_by_date.items()
            },
            "exposure_by_settlement_horizon": {
                k: str(v) for k, v in snapshot.exposure_by_settlement_horizon.items()
            },
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_exposure_snapshots (
                        id, account_id, as_of,
                        exposure_by_league_json, exposure_by_team_json,
                        exposure_by_player_json, exposure_by_event_json,
                        exposure_by_market_json, exposure_by_market_type_json,
                        exposure_by_sportsbook_json, exposure_by_outcome_json,
                        exposure_by_date_json,
                        exposure_by_settlement_horizon_json,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.id,
                        snapshot.account_id,
                        snapshot.as_of,
                        _dump(snapshot.exposure_by_league),
                        _dump(snapshot.exposure_by_team),
                        _dump(snapshot.exposure_by_player),
                        _dump(snapshot.exposure_by_event),
                        _dump(snapshot.exposure_by_market),
                        _dump(snapshot.exposure_by_market_type),
                        _dump(snapshot.exposure_by_sportsbook),
                        _dump(snapshot.exposure_by_outcome),
                        _dump(snapshot.exposure_by_date),
                        _dump(snapshot.exposure_by_settlement_horizon),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        self.record_domain_event(
            aggregate_type=AggregateType.EXPOSURE,
            aggregate_id=snapshot.id,
            event_type="exposure_recalculated",
            actor_type=ActorType.SYSTEM,
            actor_id=snapshot.account_id,
            payload=payload,
            source=SourceType.INTERNAL,
            occurred_at=snapshot.as_of,
        )
        return snapshot.id

    def append_domain_event(self, event: DomainEvent) -> str:
        self.migrate()
        canonical_payload = json.dumps(
            event.payload, sort_keys=True, separators=(",", ":")
        )
        payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        previous_hash: str | None = None

        with closing(self.connect()) as connection:
            with connection:
                prior = connection.execute(
                    """
                    SELECT event_hash
                    FROM sip_domain_events
                    WHERE aggregate_type = ? AND aggregate_id = ?
                    ORDER BY event_version DESC
                    LIMIT 1
                    """,
                    (event.aggregate_type.value, event.aggregate_id),
                ).fetchone()
                if prior is not None:
                    previous_hash = str(prior[0])

                chain_seed = "|".join(
                    [
                        event.aggregate_type.value,
                        event.aggregate_id,
                        str(event.event_version),
                        payload_hash,
                        previous_hash or "",
                    ]
                )
                event_hash = hashlib.sha256(chain_seed.encode("utf-8")).hexdigest()

                connection.execute(
                    """
                    INSERT INTO sip_domain_events (
                        id, aggregate_type, aggregate_id, event_type,
                        event_version, occurred_at, recorded_at,
                        actor_type, actor_id, correlation_id, causation_id,
                        payload_json, payload_hash, previous_event_hash, event_hash,
                        source, model_version, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.aggregate_type.value,
                        event.aggregate_id,
                        event.event_type,
                        event.event_version,
                        event.occurred_at,
                        event.recorded_at,
                        event.actor_type.value,
                        event.actor_id,
                        event.correlation_id,
                        event.causation_id,
                        canonical_payload,
                        payload_hash,
                        previous_hash,
                        event_hash,
                        event.source.value,
                        event.model_version,
                        event.schema_version,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sip_aggregate_versions (
                        aggregate_type, aggregate_id, current_version, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(aggregate_type, aggregate_id) DO UPDATE SET
                        current_version=excluded.current_version,
                        updated_at=excluded.updated_at
                    """,
                    (
                        event.aggregate_type.value,
                        event.aggregate_id,
                        event.event_version,
                        event.recorded_at,
                    ),
                )
        return event.id

    def next_aggregate_version(
        self, *, aggregate_type: AggregateType, aggregate_id: str
    ) -> int:
        self.migrate()
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT current_version
                FROM sip_aggregate_versions
                WHERE aggregate_type = ? AND aggregate_id = ?
                """,
                (aggregate_type.value, aggregate_id),
            ).fetchone()
        current = int(row[0]) if row else 0
        return current + 1

    def list_domain_events(
        self,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        correlation_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.migrate()
        query = (
            "SELECT id, aggregate_type, aggregate_id, event_type, event_version, occurred_at, recorded_at, "
            "actor_type, actor_id, correlation_id, causation_id, payload_json, payload_hash, previous_event_hash, event_hash, source, model_version, schema_version "
            "FROM sip_domain_events"
        )
        clauses = []
        params: list[Any] = []
        if aggregate_type:
            clauses.append("aggregate_type = ?")
            params.append(aggregate_type)
        if aggregate_id:
            clauses.append("aggregate_id = ?")
            params.append(aggregate_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(max(1, limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "id": str(row[0]),
                    "aggregate_type": str(row[1]),
                    "aggregate_id": str(row[2]),
                    "event_type": str(row[3]),
                    "event_version": int(row[4]),
                    "occurred_at": str(row[5]),
                    "recorded_at": str(row[6]),
                    "actor_type": str(row[7]),
                    "actor_id": str(row[8]),
                    "correlation_id": str(row[9]),
                    "causation_id": row[10],
                    "payload": json.loads(row[11]),
                    "payload_hash": str(row[12]),
                    "previous_event_hash": row[13],
                    "event_hash": str(row[14]),
                    "source": str(row[15]),
                    "model_version": row[16],
                    "schema_version": str(row[17]),
                }
            )
        return result

    def record_domain_event(
        self,
        *,
        aggregate_type: AggregateType,
        aggregate_id: str,
        event_type: str,
        actor_type: ActorType,
        actor_id: str,
        payload: dict[str, Any],
        source: SourceType = SourceType.INTERNAL,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        model_version: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        version = self.next_aggregate_version(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
        )
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        event_id = f"evt-{hashlib.sha1((aggregate_id + event_type + str(version)).encode('utf-8')).hexdigest()[:16]}"
        return self.append_domain_event(
            DomainEvent(
                id=event_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                event_version=version,
                occurred_at=occurred_at or now,
                recorded_at=now,
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id or f"corr-{event_id}",
                causation_id=causation_id,
                payload=payload,
                payload_hash=payload_hash,
                previous_event_hash=None,
                source=source,
                model_version=model_version,
                schema_version="1.0",
            )
        )

    def save_execution_order(self, order: ExecutionOrder) -> str:
        self.migrate()
        payload = {
            "id": order.id,
            "order_intent_id": order.order_intent_id,
            "account_id": order.account_id,
            "market_id": order.market_id,
            "outcome_id": order.outcome_id,
            "stake": str(order.stake),
            "requested_odds": order.requested_odds,
            "accepted_odds": order.accepted_odds,
            "sportsbook": order.sportsbook,
            "mode": order.mode.value,
            "state": order.state.value,
            "requires_confirmation": order.requires_confirmation,
            "provider_reference": order.provider_reference,
            "prefilled_url": order.prefilled_url,
            "failure_code": order.failure_code.value if order.failure_code else None,
            "failure_message": order.failure_message,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
            "metadata": order.metadata,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO sip_execution_orders (
                        id, order_intent_id, account_id, market_id, outcome_id,
                        stake, requested_odds, accepted_odds, sportsbook,
                        mode, state, requires_confirmation, provider_reference,
                        prefilled_url, failure_code, failure_message,
                        created_at, updated_at, metadata_json, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.id,
                        order.order_intent_id,
                        order.account_id,
                        order.market_id,
                        order.outcome_id,
                        str(order.stake),
                        order.requested_odds,
                        order.accepted_odds,
                        order.sportsbook,
                        order.mode.value,
                        order.state.value,
                        1 if order.requires_confirmation else 0,
                        order.provider_reference,
                        order.prefilled_url,
                        order.failure_code.value if order.failure_code else None,
                        order.failure_message,
                        order.created_at,
                        order.updated_at,
                        json.dumps(order.metadata, sort_keys=True),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        self.record_domain_event(
            aggregate_type=AggregateType.BET,
            aggregate_id=order.id,
            event_type="execution_order_upserted",
            actor_type=ActorType.SYSTEM,
            actor_id="execution-orchestrator",
            payload=payload,
            source=SourceType.INTERNAL,
            occurred_at=order.updated_at,
        )
        return order.id

    def append_execution_transition(self, transition: ExecutionTransition) -> str:
        self.migrate()
        payload = {
            "id": transition.id,
            "order_id": transition.order_id,
            "from_state": transition.from_state.value,
            "to_state": transition.to_state.value,
            "reason": transition.reason,
            "occurred_at": transition.occurred_at,
            "actor_type": transition.actor_type,
            "actor_id": transition.actor_id,
            "metadata": transition.metadata,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_execution_state_transitions (
                        id, order_id, from_state, to_state, reason,
                        occurred_at, actor_type, actor_id, metadata_json,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transition.id,
                        transition.order_id,
                        transition.from_state.value,
                        transition.to_state.value,
                        transition.reason,
                        transition.occurred_at,
                        transition.actor_type,
                        transition.actor_id,
                        json.dumps(transition.metadata, sort_keys=True),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        self.record_domain_event(
            aggregate_type=AggregateType.BET,
            aggregate_id=transition.order_id,
            event_type="execution_state_changed",
            actor_type=ActorType.SYSTEM,
            actor_id=transition.actor_id,
            payload=payload,
            source=SourceType.INTERNAL,
            occurred_at=transition.occurred_at,
        )
        return transition.id

    def save_execution_receipt(self, receipt: ExecutionReceipt) -> str:
        self.migrate()
        payload = {
            "id": receipt.id,
            "order_id": receipt.order_id,
            "receipt_type": receipt.receipt_type.value,
            "status": receipt.status,
            "created_at": receipt.created_at,
            "payload": receipt.payload,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_execution_receipts (
                        id, order_id, receipt_type, status,
                        created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.id,
                        receipt.order_id,
                        receipt.receipt_type.value,
                        receipt.status,
                        receipt.created_at,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        self.record_domain_event(
            aggregate_type=AggregateType.BET,
            aggregate_id=receipt.order_id,
            event_type="execution_receipt_recorded",
            actor_type=ActorType.SYSTEM,
            actor_id="execution-orchestrator",
            payload=payload,
            source=SourceType.INTERNAL,
            occurred_at=receipt.created_at,
        )
        return receipt.id

    def get_execution_order(self, order_id: str) -> dict[str, Any] | None:
        self.migrate()
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM sip_execution_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list_execution_orders(
        self,
        *,
        account_id: str,
        state: OrderState | None = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT payload_json FROM sip_execution_orders WHERE account_id = ?"
        params: list[Any] = [account_id]
        if state is not None:
            query += " AND state = ?"
            params.append(state.value)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_execution_transitions(self, order_id: str) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_execution_state_transitions
                WHERE order_id = ?
                ORDER BY occurred_at ASC
                """,
                (order_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_execution_receipts(self, order_id: str) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_execution_receipts
                WHERE order_id = ?
                ORDER BY created_at ASC
                """,
                (order_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_order_intent(self, intent_id: str) -> dict[str, Any] | None:
        self.migrate()
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM sip_order_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def save_order_intent_if_absent(self, intent: OrderIntent) -> str:
        existing = self.get_order_intent(intent.id)
        if existing is not None:
            return intent.id
        return self.save_order_intent(intent)

    def save_order_intent(self, intent: OrderIntent) -> str:
        self.migrate()
        payload = {
            "id": intent.id,
            "account_id": intent.account_id,
            "market_id": intent.market_id,
            "outcome_id": intent.outcome_id,
            "stake": str(intent.stake),
            "requested_odds": intent.requested_odds,
            "accepted_odds": intent.accepted_odds,
            "mode": intent.mode.value,
            "estimated_payout": str(intent.estimated_payout),
            "exposure_impact": {
                key: str(value) for key, value in intent.exposure_impact.items()
            },
            "validation_result": {
                "is_valid": intent.validation_result.is_valid,
                "issues": [
                    {"code": issue.code.value, "detail": issue.detail}
                    for issue in intent.validation_result.issues
                ],
            },
            "created_at": intent.created_at,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_order_intents (
                        id, account_id, market_id, outcome_id, stake,
                        requested_odds, accepted_odds, mode, estimated_payout,
                        exposure_impact_json, validation_result_json,
                        created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.id,
                        intent.account_id,
                        intent.market_id,
                        intent.outcome_id,
                        str(intent.stake),
                        intent.requested_odds,
                        intent.accepted_odds,
                        intent.mode.value,
                        str(intent.estimated_payout),
                        json.dumps(payload["exposure_impact"], sort_keys=True),
                        json.dumps(payload["validation_result"], sort_keys=True),
                        intent.created_at,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                self.record_audit_event(
                    entity_kind="order_intent",
                    entity_id=intent.id,
                    action="create",
                    payload=payload,
                    connection=connection,
                )
        self.record_domain_event(
            aggregate_type=AggregateType.BET,
            aggregate_id=intent.id,
            event_type="bet_draft_created",
            actor_type=ActorType.USER,
            actor_id=intent.account_id,
            payload=payload,
            source=SourceType.MANUAL_ENTRY,
            occurred_at=intent.created_at,
        )
        self.record_domain_event(
            aggregate_type=AggregateType.BET,
            aggregate_id=intent.id,
            event_type="bet_validation",
            actor_type=ActorType.SYSTEM,
            actor_id="validation-engine",
            payload=payload["validation_result"],
            source=SourceType.INTERNAL,
            occurred_at=intent.created_at,
        )
        return intent.id

    def save_bet(self, bet: Bet) -> str:
        self.migrate()
        payload = {
            "id": bet.id,
            "account_id": bet.account_id,
            "market_id": bet.market_id,
            "outcome_id": bet.outcome_id,
            "mode": bet.mode.value,
            "stake": str(bet.stake),
            "accepted_odds": bet.accepted_odds,
            "status": bet.status.value,
            "created_at": bet.created_at,
            "idempotency_key": bet.idempotency_key,
        }
        with closing(self.connect()) as connection:
            with connection:
                existing = connection.execute(
                    "SELECT id FROM sip_bets_v2 WHERE idempotency_key = ?",
                    (bet.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return str(existing[0])
                connection.execute(
                    """
                    INSERT INTO sip_bets_v2 (
                        id, account_id, market_id, outcome_id, mode,
                        stake, accepted_odds, status, created_at,
                        idempotency_key, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bet.id,
                        bet.account_id,
                        bet.market_id,
                        bet.outcome_id,
                        bet.mode.value,
                        str(bet.stake),
                        bet.accepted_odds,
                        bet.status.value,
                        bet.created_at,
                        bet.idempotency_key,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                self.record_audit_event(
                    entity_kind="bet",
                    entity_id=bet.id,
                    action="create",
                    payload=payload,
                    connection=connection,
                )
        self.record_domain_event(
            aggregate_type=AggregateType.BET,
            aggregate_id=bet.id,
            event_type="bet_recorded",
            actor_type=ActorType.USER,
            actor_id=bet.account_id,
            payload=payload,
            source=SourceType.MANUAL_ENTRY,
            occurred_at=bet.created_at,
        )
        return bet.id

    def edit_bet(
        self,
        *,
        bet_id: str,
        accepted_odds: int | None = None,
        stake: Decimal | None = None,
        actor_id: str = "system",
        correlation_id: str = "manual-edit",
    ) -> dict[str, Any]:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                row = connection.execute(
                    "SELECT payload_json FROM sip_bets_v2 WHERE id = ?",
                    (bet_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown bet: {bet_id}")
                before = json.loads(row[0])
                after = dict(before)
                if accepted_odds is not None:
                    after["accepted_odds"] = int(accepted_odds)
                if stake is not None:
                    after["stake"] = str(stake)

                connection.execute(
                    """
                    UPDATE sip_bets_v2
                    SET accepted_odds = ?, stake = ?, payload_json = ?
                    WHERE id = ?
                    """,
                    (
                        int(after["accepted_odds"]),
                        str(after["stake"]),
                        json.dumps(after, sort_keys=True),
                        bet_id,
                    ),
                )

                self.record_audit_event(
                    entity_kind="bet",
                    entity_id=bet_id,
                    action="edit",
                    payload={"before": before, "after": after},
                    connection=connection,
                )

        event_payload = {
            "bet_id": bet_id,
            "before": before,
            "after": after,
        }
        self.record_domain_event(
            aggregate_type=AggregateType.BET,
            aggregate_id=bet_id,
            event_type="bet_edit",
            actor_type=ActorType.OPERATOR,
            actor_id=actor_id,
            payload=event_payload,
            source=SourceType.MANUAL_ENTRY,
            correlation_id=correlation_id,
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
        return after

    def save_bet_legs(self, legs: tuple[BetLeg, ...] | list[BetLeg]) -> None:
        if not legs:
            return
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO sip_bet_legs (
                        id, bet_id, market_id, outcome_id,
                        accepted_odds, sequence_no, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            leg.id,
                            leg.bet_id,
                            leg.market_id,
                            leg.outcome_id,
                            leg.accepted_odds,
                            leg.sequence,
                            json.dumps(
                                {
                                    "id": leg.id,
                                    "bet_id": leg.bet_id,
                                    "market_id": leg.market_id,
                                    "outcome_id": leg.outcome_id,
                                    "accepted_odds": leg.accepted_odds,
                                    "sequence": leg.sequence,
                                },
                                sort_keys=True,
                            ),
                        )
                        for leg in legs
                    ],
                )

    def save_parlay(
        self, parlay: Parlay, legs: tuple[BetLeg, ...] | list[BetLeg]
    ) -> str:
        self.migrate()
        payload = {
            "id": parlay.id,
            "account_id": parlay.account_id,
            "mode": parlay.mode.value,
            "stake": str(parlay.stake),
            "combined_decimal_odds": str(parlay.combined_decimal_odds),
            "naive_implied_probability": str(parlay.naive_implied_probability),
            "estimated_sip_probability": (
                str(parlay.estimated_sip_probability)
                if parlay.estimated_sip_probability is not None
                else None
            ),
            "potential_return": str(parlay.potential_return),
            "expected_value": (
                str(parlay.expected_value)
                if parlay.expected_value is not None
                else None
            ),
            "correlation_warning": parlay.correlation_warning,
            "status": parlay.status.value,
            "created_at": parlay.created_at,
            "idempotency_key": parlay.idempotency_key,
            "legs": [
                {
                    "id": leg.id,
                    "market_id": leg.market_id,
                    "outcome_id": leg.outcome_id,
                    "accepted_odds": leg.accepted_odds,
                    "sequence": leg.sequence,
                }
                for leg in legs
            ],
        }
        with closing(self.connect()) as connection:
            with connection:
                existing = connection.execute(
                    "SELECT id FROM sip_parlays WHERE idempotency_key = ?",
                    (parlay.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return str(existing[0])
                connection.execute(
                    """
                    INSERT INTO sip_parlays (
                        id, account_id, mode, stake, combined_decimal_odds,
                        naive_implied_probability, estimated_sip_probability,
                        potential_return, expected_value, correlation_warning,
                        status, created_at, idempotency_key, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parlay.id,
                        parlay.account_id,
                        parlay.mode.value,
                        str(parlay.stake),
                        str(parlay.combined_decimal_odds),
                        str(parlay.naive_implied_probability),
                        str(parlay.estimated_sip_probability)
                        if parlay.estimated_sip_probability is not None
                        else None,
                        str(parlay.potential_return),
                        str(parlay.expected_value)
                        if parlay.expected_value is not None
                        else None,
                        parlay.correlation_warning,
                        parlay.status.value,
                        parlay.created_at,
                        parlay.idempotency_key,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                self.record_audit_event(
                    entity_kind="parlay",
                    entity_id=parlay.id,
                    action="create",
                    payload=payload,
                    connection=connection,
                )
        self.record_domain_event(
            aggregate_type=AggregateType.PARLAY,
            aggregate_id=parlay.id,
            event_type="parlay_created",
            actor_type=ActorType.USER,
            actor_id=parlay.account_id,
            payload=payload,
            source=SourceType.MANUAL_ENTRY,
            occurred_at=parlay.created_at,
        )
        for leg in legs:
            self.record_domain_event(
                aggregate_type=AggregateType.PARLAY,
                aggregate_id=parlay.id,
                event_type="parlay_leg_added",
                actor_type=ActorType.USER,
                actor_id=parlay.account_id,
                payload={
                    "leg_id": leg.id,
                    "market_id": leg.market_id,
                    "outcome_id": leg.outcome_id,
                    "accepted_odds": leg.accepted_odds,
                    "sequence": leg.sequence,
                },
                source=SourceType.MANUAL_ENTRY,
                occurred_at=parlay.created_at,
            )
        return parlay.id

    def save_position(self, position: Position) -> str:
        self.migrate()
        payload = {
            "id": position.id,
            "account_id": position.account_id,
            "bet_id": position.bet_id,
            "market_id": position.market_id,
            "outcome_id": position.outcome_id,
            "mode": position.mode.value,
            "stake": str(position.stake),
            "average_accepted_odds": position.average_accepted_odds,
            "entry_model_probability": (
                str(position.entry_model_probability)
                if position.entry_model_probability is not None
                else None
            ),
            "current_model_probability": (
                str(position.current_model_probability)
                if position.current_model_probability is not None
                else None
            ),
            "entry_market_probability": (
                str(position.entry_market_probability)
                if position.entry_market_probability is not None
                else None
            ),
            "current_market_probability": (
                str(position.current_market_probability)
                if position.current_market_probability is not None
                else None
            ),
            "potential_profit": str(position.potential_profit),
            "potential_return": str(position.potential_return),
            "estimated_current_value": (
                str(position.estimated_current_value)
                if position.estimated_current_value is not None
                else None
            ),
            "realized_profit_loss": str(position.realized_profit_loss),
            "status": position.status.value,
            "opened_at": position.opened_at,
            "settled_at": position.settled_at,
            "edge_at_entry": (
                str(position.edge_at_entry)
                if position.edge_at_entry is not None
                else None
            ),
            "current_edge": (
                str(position.current_edge)
                if position.current_edge is not None
                else None
            ),
            "entry_odds": position.entry_odds,
            "current_odds": position.current_odds,
            "market_type": position.market_type,
            "league": position.league,
            "event_date": position.event_date,
            "settlement_horizon": position.settlement_horizon,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO sip_positions (
                        id, account_id, bet_id, market_id, outcome_id,
                        mode, stake, average_accepted_odds,
                        entry_model_probability, current_model_probability,
                        entry_market_probability, current_market_probability,
                        potential_profit, potential_return, estimated_current_value,
                        realized_profit_loss, status, opened_at, settled_at,
                        payload_json, edge_at_entry, current_edge, entry_odds,
                        current_odds, market_type, league, event_date,
                        settlement_horizon
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position.id,
                        position.account_id,
                        position.bet_id,
                        position.market_id,
                        position.outcome_id,
                        position.mode.value,
                        str(position.stake),
                        position.average_accepted_odds,
                        str(position.entry_model_probability)
                        if position.entry_model_probability is not None
                        else None,
                        str(position.current_model_probability)
                        if position.current_model_probability is not None
                        else None,
                        str(position.entry_market_probability)
                        if position.entry_market_probability is not None
                        else None,
                        str(position.current_market_probability)
                        if position.current_market_probability is not None
                        else None,
                        str(position.potential_profit),
                        str(position.potential_return),
                        str(position.estimated_current_value)
                        if position.estimated_current_value is not None
                        else None,
                        str(position.realized_profit_loss),
                        position.status.value,
                        position.opened_at,
                        position.settled_at,
                        json.dumps(payload, sort_keys=True),
                        str(position.edge_at_entry)
                        if position.edge_at_entry is not None
                        else None,
                        str(position.current_edge)
                        if position.current_edge is not None
                        else None,
                        position.entry_odds,
                        position.current_odds,
                        position.market_type,
                        position.league,
                        position.event_date,
                        position.settlement_horizon,
                    ),
                )
                self.record_audit_event(
                    entity_kind="position",
                    entity_id=position.id,
                    action="upsert",
                    payload=payload,
                    connection=connection,
                )
        self.record_domain_event(
            aggregate_type=AggregateType.POSITION,
            aggregate_id=position.id,
            event_type=(
                "position_created"
                if position.status == PositionStatus.OPEN
                else "position_updated"
            ),
            actor_type=ActorType.USER,
            actor_id=position.account_id,
            payload=payload,
            source=SourceType.MANUAL_ENTRY,
            occurred_at=position.opened_at,
        )
        return position.id

    def save_ledger_transaction(self, transaction: LedgerTransaction) -> str:
        self.migrate()
        payload = {
            "id": transaction.id,
            "account_id": transaction.account_id,
            "position_id": transaction.position_id,
            "transaction_type": transaction.transaction_type.value,
            "amount": str(transaction.amount),
            "balance_after": str(transaction.balance_after),
            "reserved_after": str(transaction.reserved_after),
            "available_after": str(transaction.available_after),
            "note": transaction.note,
            "reference_id": transaction.reference_id,
            "created_at": transaction.created_at,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_ledger_transactions (
                        id, account_id, position_id, transaction_type,
                        amount, balance_after, reserved_after, available_after,
                        note, reference_id, created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transaction.id,
                        transaction.account_id,
                        transaction.position_id,
                        transaction.transaction_type.value,
                        str(transaction.amount),
                        str(transaction.balance_after),
                        str(transaction.reserved_after),
                        str(transaction.available_after),
                        transaction.note,
                        transaction.reference_id,
                        transaction.created_at,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                connection.execute(
                    """
                    UPDATE sip_bankroll_accounts
                    SET balance_usd = ?, held_usd = ?, available_usd = ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (
                        str(transaction.balance_after),
                        str(transaction.reserved_after),
                        str(transaction.available_after),
                        transaction.created_at,
                        transaction.account_id,
                    ),
                )
                self.record_audit_event(
                    entity_kind="ledger_transaction",
                    entity_id=transaction.id,
                    action="append",
                    payload=payload,
                    connection=connection,
                )
        self.record_domain_event(
            aggregate_type=AggregateType.LEDGER,
            aggregate_id=transaction.id,
            event_type=(
                "stake_reservation"
                if transaction.transaction_type
                == LedgerTransactionType.STAKE_RESERVATION
                else "ledger_adjustment"
            ),
            actor_type=ActorType.USER,
            actor_id=transaction.account_id,
            payload=payload,
            source=SourceType.INTERNAL,
            occurred_at=transaction.created_at,
        )
        return transaction.id

    def save_settlement_v2(self, settlement: Settlement) -> str:
        self.migrate()
        payload = {
            "id": settlement.id,
            "position_id": settlement.position_id,
            "market_id": settlement.market_id,
            "outcome_id": settlement.outcome_id,
            "result": settlement.result.value,
            "payout": str(settlement.payout),
            "realized_profit_loss": str(settlement.realized_profit_loss),
            "settled_at": settlement.settled_at,
            "resolution_source": settlement.resolution_source,
            "notes": settlement.notes,
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_settlements_v2 (
                        id, position_id, market_id, outcome_id, result,
                        payout, realized_profit_loss, settled_at,
                        resolution_source, notes, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        settlement.id,
                        settlement.position_id,
                        settlement.market_id,
                        settlement.outcome_id,
                        settlement.result.value,
                        str(settlement.payout),
                        str(settlement.realized_profit_loss),
                        settlement.settled_at,
                        settlement.resolution_source,
                        settlement.notes,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                connection.execute(
                    """
                    UPDATE sip_positions
                    SET status = ?, settled_at = ?, realized_profit_loss = ?,
                        payload_json = json_set(
                            payload_json,
                            '$.status', ?,
                            '$.settled_at', ?,
                            '$.realized_profit_loss', ?
                        )
                    WHERE id = ?
                    """,
                    (
                        PositionStatus.SETTLED.value,
                        settlement.settled_at,
                        str(settlement.realized_profit_loss),
                        PositionStatus.SETTLED.value,
                        settlement.settled_at,
                        str(settlement.realized_profit_loss),
                        settlement.position_id,
                    ),
                )
                self.record_audit_event(
                    entity_kind="settlement_v2",
                    entity_id=settlement.id,
                    action="record",
                    payload=payload,
                    connection=connection,
                )
        self.record_domain_event(
            aggregate_type=AggregateType.SETTLEMENT,
            aggregate_id=settlement.id,
            event_type="settlement_completed",
            actor_type=ActorType.SYSTEM,
            actor_id="settlement-engine",
            payload=payload,
            source=SourceType.SETTLEMENT_FEED,
            occurred_at=settlement.settled_at,
        )
        return settlement.id

    def list_positions(
        self,
        *,
        account_id: str,
        status: PositionStatus | None = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        self.migrate()
        query = "SELECT payload_json FROM sip_positions WHERE account_id = ?"
        params: list[Any] = [account_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY opened_at DESC LIMIT ?"
        params.append(max(1, limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_ledger_transactions(
        self, *, account_id: str, limit: int = 250
    ) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_ledger_transactions
                WHERE account_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (account_id, max(1, limit)),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_activity_feed(
        self,
        *,
        account_id: str,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            transactions = connection.execute(
                """
                SELECT created_at, 'ledger_transaction' AS kind, payload_json
                FROM sip_ledger_transactions
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchall()
            positions = connection.execute(
                """
                SELECT opened_at AS created_at, 'position' AS kind, payload_json
                FROM sip_positions
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchall()
            settlements = connection.execute(
                """
                SELECT s.settled_at AS created_at, 'settlement' AS kind, s.payload_json
                FROM sip_settlements_v2 s
                JOIN sip_positions p ON p.id = s.position_id
                WHERE p.account_id = ?
                """,
                (account_id,),
            ).fetchall()
            order_intents = connection.execute(
                """
                SELECT created_at, 'order_intent' AS kind, payload_json
                FROM sip_order_intents
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchall()
            domain_events = connection.execute(
                """
                SELECT recorded_at AS created_at, 'domain_event' AS kind, payload_json
                FROM sip_domain_events
                WHERE actor_id = ? OR json_extract(payload_json, '$.account_id') = ?
                """,
                (account_id, account_id),
            ).fetchall()

        rows = [
            *transactions,
            *positions,
            *settlements,
            *order_intents,
            *domain_events,
        ]
        events = [
            {
                "created_at": str(row[0]),
                "kind": str(row[1]),
                "payload": json.loads(row[2]),
            }
            for row in rows
        ]
        events.sort(key=lambda item: str(item["created_at"]), reverse=True)
        return events[: max(1, limit)]

    def portfolio_summary(self, account_id: str) -> dict[str, Any]:
        self.migrate()
        account = self.get_bankroll_account(account_id)
        open_positions = self.list_positions(
            account_id=account_id,
            status=PositionStatus.OPEN,
            limit=1000,
        )
        settled_positions = self.list_positions(
            account_id=account_id,
            status=PositionStatus.SETTLED,
            limit=1000,
        )

        open_stake = sum(
            (Decimal(str(item["stake"])) for item in open_positions),
            Decimal("0"),
        )
        max_loss = open_stake
        max_profit = sum(
            (Decimal(str(item["potential_profit"])) for item in open_positions),
            Decimal("0"),
        )
        realized = sum(
            (Decimal(str(item["realized_profit_loss"])) for item in settled_positions),
            Decimal("0"),
        )
        reserved = account.held_usd if account is not None else Decimal("0")
        cash = account.available_usd if account is not None else Decimal("0")
        balance = account.balance_usd if account is not None else Decimal("0")
        portfolio_value = (cash + reserved + realized).quantize(Decimal("0.01"))

        exposure_by_league: dict[str, Decimal] = {}
        exposure_by_market: dict[str, Decimal] = {}
        exposure_by_event: dict[str, Decimal] = {}
        exposure_by_team: dict[str, Decimal] = {}
        exposure_by_player: dict[str, Decimal] = {}
        exposure_by_market_type: dict[str, Decimal] = {}
        exposure_by_sportsbook: dict[str, Decimal] = {}
        exposure_by_outcome: dict[str, Decimal] = {}
        exposure_by_date: dict[str, Decimal] = {}
        exposure_by_horizon: dict[str, Decimal] = {}

        deposits = Decimal("0")
        withdrawals = Decimal("0")
        opening_bankroll = Decimal("0")
        push_count = 0
        void_count = 0
        synthetic_market_change = Decimal("0")
        synthetic_sip_change = Decimal("0")

        with closing(self.connect()) as connection:
            tx_rows = connection.execute(
                """
                SELECT transaction_type, amount
                FROM sip_ledger_transactions
                WHERE account_id = ?
                ORDER BY created_at ASC
                """,
                (account_id,),
            ).fetchall()
            if tx_rows:
                opening_bankroll = Decimal(str(account.balance_usd if account else 0))
                running_delta = sum(
                    (Decimal(str(item[1])) for item in tx_rows),
                    Decimal("0"),
                )
                opening_bankroll = (opening_bankroll - running_delta).quantize(
                    Decimal("0.01")
                )
            for tx_type, amount_text in tx_rows:
                amount = Decimal(str(amount_text))
                if str(tx_type) == LedgerTransactionType.DEPOSIT.value:
                    deposits += amount
                if str(tx_type) == LedgerTransactionType.WITHDRAWAL.value:
                    withdrawals += abs(amount)

            for position in open_positions:
                market_id = str(position["market_id"])
                row = connection.execute(
                    """
                    SELECT league, canonical_event_id, market_type, closes_at
                    FROM sip_markets
                    WHERE id = ?
                    """,
                    (market_id,),
                ).fetchone()
                amount = Decimal(str(position["stake"]))
                if row is not None:
                    league = str(row[0])
                    event_id = str(row[1])
                    market_type = str(row[2])
                    close_date = str(row[3])[:10]
                    exposure_by_league[league] = (
                        exposure_by_league.get(league, Decimal("0")) + amount
                    )
                    exposure_by_event[event_id] = (
                        exposure_by_event.get(event_id, Decimal("0")) + amount
                    )
                    exposure_by_market_type[market_type] = (
                        exposure_by_market_type.get(market_type, Decimal("0")) + amount
                    )
                    exposure_by_date[close_date] = (
                        exposure_by_date.get(close_date, Decimal("0")) + amount
                    )

                settlement_horizon = str(
                    position.get("settlement_horizon") or "unknown"
                )
                exposure_by_horizon[settlement_horizon] = (
                    exposure_by_horizon.get(settlement_horizon, Decimal("0")) + amount
                )

                exposure_by_market[market_id] = (
                    exposure_by_market.get(market_id, Decimal("0")) + amount
                )
                outcome_key = str(position["outcome_id"])
                exposure_by_outcome[outcome_key] = (
                    exposure_by_outcome.get(outcome_key, Decimal("0")) + amount
                )

                outcome_id = str(position["outcome_id"])
                team_row = connection.execute(
                    "SELECT team_id, player_id FROM sip_outcomes WHERE id = ?",
                    (outcome_id,),
                ).fetchone()
                if team_row is not None and team_row[0]:
                    team = str(team_row[0])
                    exposure_by_team[team] = (
                        exposure_by_team.get(team, Decimal("0")) + amount
                    )
                if team_row is not None and team_row[1]:
                    player = str(team_row[1])
                    exposure_by_player[player] = (
                        exposure_by_player.get(player, Decimal("0")) + amount
                    )

                sportsbook_row = connection.execute(
                    """
                    SELECT sportsbook
                    FROM sip_probability_snapshots
                    WHERE market_id = ? AND outcome_id = ?
                    ORDER BY quote_timestamp DESC
                    LIMIT 1
                    """,
                    (market_id, outcome_id),
                ).fetchone()
                if sportsbook_row is not None and sportsbook_row[0]:
                    book = str(sportsbook_row[0])
                    exposure_by_sportsbook[book] = (
                        exposure_by_sportsbook.get(book, Decimal("0")) + amount
                    )

                valuation_row = connection.execute(
                    """
                    SELECT estimated_change_since_entry_market,
                           estimated_change_since_entry_sip
                    FROM sip_synthetic_position_valuations
                    WHERE position_id = ?
                    ORDER BY valuation_timestamp DESC
                    LIMIT 1
                    """,
                    (str(position["id"]),),
                ).fetchone()
                if valuation_row is not None:
                    synthetic_market_change += Decimal(str(valuation_row[0]))
                    synthetic_sip_change += Decimal(str(valuation_row[1]))

            for position in settled_positions:
                status = str(position.get("status") or "")
                if status == PositionStatus.VOID.value:
                    void_count += 1

            push_rows = connection.execute(
                """
                SELECT COUNT(*)
                FROM sip_settlements_v2
                WHERE result = 'push'
                """
            ).fetchone()
            push_count = int(push_rows[0]) if push_rows else 0

        return {
            "account_id": account_id,
            "opening_bankroll": str(opening_bankroll.quantize(Decimal("0.01"))),
            "deposits": str(deposits.quantize(Decimal("0.01"))),
            "withdrawals": str(withdrawals.quantize(Decimal("0.01"))),
            "cash_balance": str(cash.quantize(Decimal("0.01"))),
            "reserved_balance": str(reserved.quantize(Decimal("0.01"))),
            "portfolio_value": str(portfolio_value),
            "open_stake": str(open_stake.quantize(Decimal("0.01"))),
            "maximum_possible_loss": str(max_loss.quantize(Decimal("0.01"))),
            "maximum_possible_profit": str(max_profit.quantize(Decimal("0.01"))),
            "realized_profit_loss": str(realized.quantize(Decimal("0.01"))),
            "unrealized_synthetic_market_change": str(
                synthetic_market_change.quantize(Decimal("0.01"))
            ),
            "unrealized_sip_adjusted_change": str(
                synthetic_sip_change.quantize(Decimal("0.01"))
            ),
            "open_positions": len(open_positions),
            "settled_positions": len(settled_positions),
            "voided_wagers": void_count,
            "pushed_wagers": push_count,
            "exposure": {
                "league": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_league.items()
                },
                "team": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_team.items()
                },
                "player": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_player.items()
                },
                "market": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_market.items()
                },
                "event": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_event.items()
                },
                "market_type": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_market_type.items()
                },
                "sportsbook": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_sportsbook.items()
                },
                "outcome": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_outcome.items()
                },
                "date": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_date.items()
                },
                "settlement_horizon": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in exposure_by_horizon.items()
                },
            },
            "has_persisted_data": account is not None,
            "balance_usd": str(balance.quantize(Decimal("0.01"))),
        }

    def record_audit_event(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        action: str,
        payload: dict[str, Any],
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            self.migrate()
        owns_connection = connection is None
        active = connection or self.connect()
        try:
            active.execute(
                """
                INSERT INTO sip_audit_log (
                    entity_kind, entity_id, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    entity_kind,
                    entity_id,
                    action,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            if owns_connection:
                active.commit()
        finally:
            if owns_connection:
                active.close()

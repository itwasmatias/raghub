from dataclasses import dataclass
from datetime import datetime


@dataclass
class Observation:
    subject: str
    description: str
    source: str
    timestamp: datetime
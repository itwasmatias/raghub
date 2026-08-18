# MissionaryX Environment Setup

This document provides instructions for creating a reproducible Python development environment for MissionaryX.

---

## Requirements

**Python Version**: 3.11 or newer (tested with 3.14.6)

**Operating System**:
- Fedora Linux (primary development environment)
- Other Linux distributions (should work with minor adjustments)
- Windows (Windows HP 14 development machine - separate setup)

**Dependencies**: Managed via `requirements-fedora-core.txt` and `requirements-dev.txt`

---

## Quick Setup

From the repository root:

```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate

# Install development dependencies
pip install -r requirements-dev.txt
```

The `requirements-dev.txt` automatically includes `requirements-fedora-core.txt`.

---

## Detailed Setup

### 1. Navigate to Repository

```bash
cd /path/to/missionaryx-worktree
```

### 2. Create Virtual Environment

```bash
python3 -m venv .venv
```

This creates an isolated Python environment in `.venv/` directory.

**Important**: The `.venv/` directory is git-ignored and should not be committed.

### 3. Activate Virtual Environment

**On Linux/macOS**:
```bash
source .venv/bin/activate
```

**On Windows**:
```bash
.venv\Scripts\activate
```

After activation, your shell prompt should show `(.venv)` prefix.

### 4. Upgrade pip (Optional but Recommended)

```bash
pip install --upgrade pip
```

### 5. Install Dependencies

**For development** (includes test dependencies):
```bash
pip install -r requirements-dev.txt
```

**For production only** (core dependencies only):
```bash
pip install -r requirements-fedora-core.txt
```

### 6. Verify Installation

```bash
# Check Python version
python --version
# Should be 3.11 or newer

# Check pytest is available
pytest --version
# Should show pytest version

# List installed packages
pip list
```

---

## Dependency Structure

### requirements-fedora-core.txt

Core production dependencies:

```
beautifulsoup4
feedparser
Flask
openai
psycopg
psycopg-binary
pypdf
python-dotenv
requests
tiktoken
```

**Important**: NumPy, pandas, SciPy, and scikit-learn are **intentionally excluded** on Fedora. These dependencies are isolated to the optional Windows worker for hybrid compute. See `docs/HYBRID_COMPUTE.md` for details.

### requirements-dev.txt

Development and testing dependencies:

```
-r requirements-fedora-core.txt
pytest==9.1.1
```

This includes all core dependencies plus test framework.

---

## Running Tests Without Activation

You can run tests without activating the virtual environment by using explicit paths:

```bash
# Run pytest
.venv/bin/pytest -q

# Run Python script
.venv/bin/python script.py

# Run module
.venv/bin/python -m module.name
```

This is useful for:
- Automation scripts
- CI/CD environments
- Validation tools

---

## Verification Steps

After setup, verify your environment:

```bash
# 1. Python version
.venv/bin/python --version

# 2. Pytest version
.venv/bin/pytest --version

# 3. Import key dependencies
.venv/bin/python -c "import flask; import openai; import pytest; print('OK')"

# 4. Run quick validation
./tools/validate-missionaryx --quick
```

All commands should succeed without errors.

---

## Troubleshooting

### Virtual Environment Not Created

**Problem**: `python3 -m venv .venv` fails

**Solution**:
```bash
# Install venv package (if not included in your Python distribution)
sudo dnf install python3-venv  # Fedora
sudo apt install python3-venv  # Ubuntu/Debian
```

### Pip Install Fails

**Problem**: `pip install -r requirements-dev.txt` fails

**Possible causes**:
1. Network connectivity issues
2. Missing system dependencies
3. Incompatible Python version

**Solutions**:
```bash
# Check Python version (must be 3.11+)
python3 --version

# Upgrade pip
pip install --upgrade pip

# Install system dependencies (Fedora)
sudo dnf install gcc python3-devel

# Try installing packages one at a time to identify the problem
pip install pytest
```

### Import Errors After Installation

**Problem**: `import flask` or similar fails

**Solution**:
```bash
# Ensure virtual environment is activated
source .venv/bin/activate

# Verify package is installed
pip show flask

# Reinstall if needed
pip install --force-reinstall flask
```

### Tests Fail Due to Missing Dependencies

**Problem**: Tests fail with `ModuleNotFoundError`

**Solution**:
```bash
# Ensure you installed dev dependencies, not just core
pip install -r requirements-dev.txt

# Verify pytest is available
pytest --version
```

---

## Environment Isolation

### Why Virtual Environments?

Virtual environments provide:
- **Isolation**: Dependencies don't conflict with system packages
- **Reproducibility**: Same environment across different machines
- **Safety**: No need for `sudo pip install` (dangerous!)
- **Version control**: Pin specific versions for stability

### What Gets Installed Where?

**System Python**: `/usr/bin/python3` (managed by system package manager)

**Virtual environment Python**: `.venv/bin/python` (isolated)

**System packages**: `/usr/lib/python3.x/site-packages`

**Virtual environment packages**: `.venv/lib/python3.x/site-packages`

**Best practice**: Always use virtual environments for development.

---

## Updating Dependencies

### Adding New Dependencies

```bash
# Activate environment
source .venv/bin/activate

# Install new package
pip install new-package

# Update requirements file
pip freeze | grep new-package >> requirements-fedora-core.txt
```

**Important**: Only add core runtime dependencies to `requirements-fedora-core.txt`. Test-only dependencies go in `requirements-dev.txt`.

### Upgrading Existing Dependencies

```bash
# Activate environment
source .venv/bin/activate

# Upgrade specific package
pip install --upgrade package-name

# Update requirements file
# (Edit requirements-fedora-core.txt manually with new version)
```

**Warning**: Be cautious when upgrading. Run full validation after any dependency changes.

---

## Makefile Integration

The repository `Makefile` includes environment setup:

```bash
# Create environment and install dependencies
make setup

# This runs:
# python3 -m venv .venv
# .venv/bin/pip install -r requirements-fedora-core.txt
# (plus other setup tasks)
```

See the `Makefile` for other available targets.

---

## Development Workflow

### Standard Workflow

```bash
# 1. Create environment (once)
python3 -m venv .venv

# 2. Activate (each session)
source .venv/bin/activate

# 3. Install/update dependencies (as needed)
pip install -r requirements-dev.txt

# 4. Work on code
# ... make changes ...

# 5. Run tests
pytest -q

# 6. Deactivate when done
deactivate
```

### Without Activation

```bash
# Run tests without activation
.venv/bin/pytest -q

# Run validation without activation
./tools/validate-missionaryx --full

# Run any Python script without activation
.venv/bin/python script.py
```

---

## CI/CD Considerations

For automated environments:

```bash
#!/bin/bash
# ci-setup.sh

# Use explicit Python version
python3 -m venv .venv

# Upgrade pip for latest features
.venv/bin/pip install --upgrade pip

# Install dependencies
.venv/bin/pip install -r requirements-dev.txt

# Verify installation
.venv/bin/python --version
.venv/bin/pytest --version

# Run validation
./tools/validate-missionaryx --full
```

---

## Environment Variables

MissionaryX uses environment variables for configuration. See `.env.example` for template:

```bash
# Copy example
cp .env.example .env

# Edit configuration
vi .env

# NEVER commit .env to repository!
```

Common variables:
- `RAGHUB_SECRET_KEY`: Application secret key
- API keys for various services
- Database paths
- Feature flags

---

## Platform-Specific Notes

### Fedora HP Pavilion (Primary Development)

- Python 3.14.6 available
- No AVX support (affects local model binaries)
- NumPy/pandas/scikit-learn excluded from core deps
- Uses requirements-fedora-core.txt

### Windows HP 14 (Windows Worker)

- Separate environment setup
- Includes NumPy, pandas, scikit-learn
- Uses requirements-windows-worker.txt
- See `docs/HYBRID_COMPUTE.md`

---

## Summary: Environment Setup Checklist

- [ ] Python 3.11 or newer installed
- [ ] Created `.venv` virtual environment
- [ ] Installed dependencies: `pip install -r requirements-dev.txt`
- [ ] Verified Python version: `.venv/bin/python --version`
- [ ] Verified pytest available: `.venv/bin/pytest --version`
- [ ] Ran quick validation: `./tools/validate-missionaryx --quick`
- [ ] Environment works correctly

**Your environment is ready for MissionaryX development!**

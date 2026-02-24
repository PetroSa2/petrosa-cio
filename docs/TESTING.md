# Testing Guide

## Overview

This service uses pytest for testing with a focus on comprehensive coverage, fast execution, and clear organization. Tests are categorized into unit, integration, and end-to-end tests.

## Test Organization

```
tests/
├── unit/              # Unit tests (isolated, no external dependencies)
├── integration/       # Integration tests (with external services)
├── e2e/              # End-to-end tests (full system)
├── conftest.py       # Shared fixtures and configuration
└── __init__.py
```

### Test Categories

#### Unit Tests (`tests/unit/`)

**Purpose**: Test individual functions, classes, or modules in isolation

**Characteristics:**
- No external dependencies (DB, APIs, filesystems)
- Use mocks/stubs for dependencies
- Fast execution (< 10s for entire suite)
- High coverage of business logic

**Example:**
```python
# tests/unit/test_calculator.py
import pytest
from mymodule import Calculator

@pytest.mark.unit
def test_calculator_add_positive_numbers():
    calc = Calculator()
    result = calc.add(2, 3)
    assert result == 5
```

#### Integration Tests (`tests/integration/`)

**Purpose**: Test interactions between components and external services

**Characteristics:**
- Tests with real databases, APIs, message queues
- Slower than unit tests
- Verify component integration
- Use test databases/services

**Example:**
```python
# tests/integration/test_database.py
import pytest
from mymodule import DatabaseClient

@pytest.mark.integration
def test_database_save_and_retrieve(test_db):
    client = DatabaseClient(test_db)
    client.save({"id": 1, "name": "test"})
    result = client.get(1)
    assert result["name"] == "test"
```

#### End-to-End Tests (`tests/e2e/`)

**Purpose**: Test complete user workflows through the system

**Characteristics:**
- Test entire application flow
- Slowest tests
- Most realistic scenarios
- May require full system setup

**Example:**
```python
# tests/e2e/test_workflow.py
import pytest
from mymodule import Application

@pytest.mark.e2e
@pytest.mark.slow
def test_complete_user_workflow(app_client):
    # Create user
    response = app_client.post("/users", json={"name": "test"})
    assert response.status_code == 201
    
    # Login
    response = app_client.post("/login", json={"name": "test"})
    assert response.status_code == 200
    
    # Perform action
    response = app_client.get("/data")
    assert response.status_code == 200
```

## Running Tests

### Run All Tests
```bash
make test  # Runs all tests with coverage, fails if < 40%
```

### Run Specific Test Categories
```bash
make unit          # Unit tests only
make integration   # Integration tests only
make e2e          # End-to-end tests only
```

### Run Specific Test Files
```bash
pytest tests/unit/test_calculator.py -v
```

### Run Specific Test Functions
```bash
pytest tests/unit/test_calculator.py::test_calculator_add -v
```

### Run Tests Matching Pattern
```bash
pytest -k "calculator" -v  # Runs all tests with "calculator" in name
```

## Coverage

### Coverage Requirements
- **Minimum**: 40% (enforced in CI)
- **Target**: 60%
- **Goal**: 80%

### Generate Coverage Reports
```bash
make coverage  # Generate coverage report
```

### View HTML Coverage Report
```bash
make coverage
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

### Check Coverage for Specific Module
```bash
pytest tests/ --cov=mymodule --cov-report=term-missing
```

## Test Fixtures

### Shared Fixtures (`conftest.py`)

Define reusable fixtures in `tests/conftest.py`:

```python
# tests/conftest.py
import pytest
from mymodule import Application, DatabaseClient

@pytest.fixture
def app():
    """Create application instance for testing"""
    app = Application(config="test")
    yield app
    app.cleanup()

@pytest.fixture
def db_client():
    """Create database client with test database"""
    client = DatabaseClient(database="test_db")
    yield client
    client.close()
    client.cleanup_test_data()

@pytest.fixture
def sample_data():
    """Provide sample data for tests"""
    return {
        "id": 1,
        "name": "test",
        "values": [1, 2, 3]
    }
```

### Using Fixtures
```python
def test_with_fixtures(app, db_client, sample_data):
    db_client.save(sample_data)
    result = app.process(sample_data["id"])
    assert result is not None
```

## Test Markers

### Available Markers
- `@pytest.mark.unit`: Unit tests
- `@pytest.mark.integration`: Integration tests
- `@pytest.mark.e2e`: End-to-end tests
- `@pytest.mark.slow`: Tests taking > 1s

### Using Markers
```python
import pytest

@pytest.mark.unit
def test_fast_unit():
    assert True

@pytest.mark.integration
@pytest.mark.slow
def test_slow_integration():
    # Long-running integration test
    pass
```

### Run Tests by Marker
```bash
pytest -m unit  # Run only unit tests
pytest -m "not slow"  # Skip slow tests
pytest -m "unit or integration"  # Run unit OR integration
```

## Mocking

### Mock External Dependencies
```python
from unittest.mock import Mock, patch
import pytest

@pytest.mark.unit
def test_with_mock():
    with patch('mymodule.external_api_call') as mock_api:
        mock_api.return_value = {"status": "success"}
        
        result = my_function()
        
        assert result["status"] == "success"
        mock_api.assert_called_once()
```

### Mock Fixtures
```python
@pytest.fixture
def mock_database():
    mock_db = Mock()
    mock_db.query.return_value = [{"id": 1}]
    return mock_db

def test_with_mock_db(mock_database):
    result = process_query(mock_database)
    assert len(result) == 1
```

## Test Naming Conventions

### Pattern
`test_<function>_<scenario>_<expected_result>`

### Examples
```python
def test_calculator_add_positive_numbers_returns_sum():
    pass

def test_user_login_invalid_credentials_raises_error():
    pass

def test_api_endpoint_missing_params_returns_400():
    pass

def test_database_save_duplicate_key_raises_exception():
    pass
```

## Parametrized Tests

### Test Multiple Inputs
```python
import pytest

@pytest.mark.parametrize("a,b,expected", [
    (1, 2, 3),
    (0, 0, 0),
    (-1, 1, 0),
    (100, 200, 300),
])
def test_calculator_add(a, b, expected):
    calc = Calculator()
    assert calc.add(a, b) == expected
```

## Async Tests

### Testing Async Functions
```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    result = await my_async_function()
    assert result == expected_value
```

## Test Data Management

### Use Factories for Test Data
```python
class UserFactory:
    @staticmethod
    def create(name="test", email="test@example.com"):
        return {
            "name": name,
            "email": email,
            "created_at": "2024-01-01"
        }

def test_with_factory():
    user = UserFactory.create(name="John")
    assert user["name"] == "John"
```

## Best Practices

### DO

✅ **Write tests first** (TDD) when possible
✅ **Keep tests independent** - each test should run in isolation
✅ **Use descriptive test names** that explain what is being tested
✅ **Test edge cases** and error conditions
✅ **Mock external dependencies** in unit tests
✅ **Clean up** test data after each test
✅ **Use fixtures** for reusable test setup
✅ **Aim for fast tests** - unit tests should be < 100ms each

### DON'T

❌ **Don't test external libraries** - focus on your code
❌ **Don't make tests dependent** on each other
❌ **Don't use production databases** in tests
❌ **Don't skip tests** without a good reason
❌ **Don't test implementation details** - test behavior
❌ **Don't have large test files** - split into logical modules
❌ **Don't ignore failing tests** - fix or remove them

## Continuous Integration

### Test Execution in CI

Tests run automatically on every PR:
1. Lint and format checks
2. Type checking
3. All tests with coverage
4. Security scanning

**CI Requirements:**
- All tests must pass
- Coverage must be ≥ 40%
- No critical security issues

### Pre-Push Checklist

Before pushing code:
```bash
make format      # Format code
make lint        # Check linting
make test        # Run all tests
make security    # Run security scans
```

Or run the full pipeline:
```bash
make pipeline
```

## Debugging Tests

### Run Tests with Debug Output
```bash
pytest -vv -s tests/  # Very verbose with print statements
```

### Drop into Debugger on Failure
```bash
pytest --pdb tests/  # Drop into pdb on first failure
```

### Debug Specific Test
```bash
pytest tests/unit/test_file.py::test_function -vv -s --pdb
```

### Show Test Durations
```bash
pytest --durations=10  # Show 10 slowest tests
```

## Common Issues

### Issue: Tests Pass Locally but Fail in CI

**Causes:**
- Environment differences
- Missing dependencies
- Test order dependencies
- Timezone/locale differences

**Solutions:**
- Run tests in isolation: `pytest --forked`
- Check for test interdependencies
- Use Docker to match CI environment

### Issue: Low Coverage

**Solutions:**
- Identify uncovered lines: `pytest --cov-report=term-missing`
- Add tests for edge cases
- Test error handling paths
- Remove dead code

### Issue: Slow Tests

**Solutions:**
- Use `pytest-xdist` for parallel execution: `pytest -n auto`
- Move slow tests to integration/e2e categories
- Mock external services
- Optimize test fixtures

## Related Documentation

- [CI/CD Pipeline](./CI_CD_PIPELINE.md)
- [Makefile Reference](./MAKEFILE.md)
- [Quick Reference](./QUICK_REFERENCE.md)


# Makefile Reference

## Overview

This service uses a standardized Makefile (v2.0) that provides consistent development commands across all Petrosa services. The Makefile automates common tasks like testing, linting, building, and deployment.

## Quick Start

```bash
make help     # Show all available commands
make setup    # Complete environment setup
make pipeline # Run full CI/CD pipeline locally
```

## Commands Reference

### Setup & Installation

#### `make setup`
**Purpose**: Complete environment setup with all dependencies and pre-commit hooks

**What it does:**
- Upgrades pip to latest version
- Installs production dependencies from `requirements.txt`
- Installs development dependencies from `requirements-dev.txt`
- Installs pre-commit git hooks

**When to use:**
- First time setting up the project
- After cloning the repository
- When dependencies have changed significantly

**Example:**
```bash
make setup
```

#### `make install`
**Purpose**: Install only production dependencies

**What it does:**
- Installs packages from `requirements.txt`

**When to use:**
- Production environments
- Docker builds
- When you only need runtime dependencies

**Example:**
```bash
make install
```

#### `make install-dev`
**Purpose**: Install development dependencies

**What it does:**
- Installs packages from `requirements-dev.txt`

**When to use:**
- Development environments
- CI/CD pipelines
- When you need testing and linting tools

**Example:**
```bash
make install-dev
```

#### `make clean`
**Purpose**: Clean up cache and temporary files

**What it does:**
- Removes `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- Removes coverage reports (`htmlcov/`, `.coverage`, `coverage.xml`)
- Removes security scan outputs (`.trivy/`, `bandit-report.json`)
- Removes Python bytecode files (`*.pyc`)
- Removes `__pycache__` directories
- Removes `*.egg-info` directories

**When to use:**
- Before running a fresh pipeline
- When disk space is low
- To reset the environment

**Example:**
```bash
make clean
```

### Code Quality

#### `make format`
**Purpose**: Auto-format code to comply with style standards

**What it does:**
- Runs black code formatter (line length: 88)
- Runs isort import sorter (black-compatible profile)

**When to use:**
- Before committing code
- After writing new code
- To fix formatting issues

**Example:**
```bash
make format
```

**Note**: This command modifies files in place.

#### `make lint`
**Purpose**: Check code for errors and style violations

**What it does:**
- Runs flake8 for critical errors (E9, F63, F7, F82)
- Runs flake8 for style issues (max complexity: 10, line length: 88)
- Runs ruff linter with auto-fix

**When to use:**
- Before committing code
- Before creating a PR
- To check code quality

**Example:**
```bash
make lint
```

**Exit codes:**
- 0: No errors
- 1: Errors found

#### `make type-check`
**Purpose**: Run static type checking

**What it does:**
- Runs mypy type checker
- Ignores missing imports

**When to use:**
- Before committing code
- To catch type-related bugs early
- As part of code review

**Example:**
```bash
make type-check
```

**Note**: Initially set to not fail CI, but warnings should be addressed.

#### `make pre-commit`
**Purpose**: Run all pre-commit hooks on all files

**What it does:**
- Executes all configured pre-commit hooks
- Runs checks like trailing whitespace, EOF fixers, YAML validation

**When to use:**
- Before committing (automatic if hooks are installed)
- To manually run all hooks
- To validate code before PR

**Example:**
```bash
make pre-commit
```

### Testing

#### `make test`
**Purpose**: Run all tests with coverage enforcement

**What it does:**
- Runs pytest on `tests/` directory
- Generates coverage reports (term, HTML, XML)
- Fails if coverage is below 40%

**When to use:**
- Before committing code
- Before creating a PR
- To verify all tests pass

**Example:**
```bash
make test
```

**Exit codes:**
- 0: All tests passed, coverage ≥ 40%
- 1: Tests failed OR coverage < 40%

**Output:**
- Terminal report with missing lines
- HTML report in `htmlcov/`
- XML report in `coverage.xml`

#### `make unit`
**Purpose**: Run only unit tests

**What it does:**
- Runs tests marked with `@pytest.mark.unit`

**When to use:**
- Quick feedback during development
- Testing specific functionality
- Fast iteration

**Example:**
```bash
make unit
```

#### `make integration`
**Purpose**: Run only integration tests

**What it does:**
- Runs tests marked with `@pytest.mark.integration`

**When to use:**
- Testing component interactions
- After unit tests pass
- Before deployment

**Example:**
```bash
make integration
```

#### `make e2e`
**Purpose**: Run only end-to-end tests

**What it does:**
- Runs tests marked with `@pytest.mark.e2e`

**When to use:**
- Full system verification
- Before production deployment
- Regression testing

**Example:**
```bash
make e2e
```

#### `make coverage`
**Purpose**: Generate coverage reports without failing on threshold

**What it does:**
- Runs pytest with coverage
- Generates reports (term, HTML, XML)
- Does NOT fail if coverage is low

**When to use:**
- Investigating current coverage levels
- Generating reports for analysis
- CI/CD coverage tracking

**Example:**
```bash
make coverage
open htmlcov/index.html  # View HTML report
```

### Security

#### `make security`
**Purpose**: Run security scans on code and dependencies

**What it does:**
- Runs bandit for Python code security issues
- Runs trivy for filesystem vulnerabilities (if installed)

**When to use:**
- Before committing code
- Before deployment
- Regular security audits

**Example:**
```bash
make security
```

**Output:**
- `bandit-report.json`: Bandit findings
- Terminal output from trivy

**Note**: Trivy is optional and will show a warning if not installed.

### Docker

#### `make build`
**Purpose**: Build Docker image for the service

**What it does:**
- Builds Docker image using `Dockerfile`
- Tags image as `$(IMAGE_NAME):latest`

**When to use:**
- Before testing container
- Before deployment
- After code changes

**Example:**
```bash
make build
```

**Variables:**
- `IMAGE_NAME`: Derived from directory name (basename)

#### `make container`
**Purpose**: Test that the Docker container works

**What it does:**
- Runs container with a simple Python command
- Exits immediately after test

**When to use:**
- After building image
- To verify container functionality
- Before pushing to registry

**Example:**
```bash
make container
```

**Exit codes:**
- 0: Container works
- 1: Container failed to start or run

### Kubernetes Deployment

#### `make deploy`
**Purpose**: Deploy service to Kubernetes cluster

**What it does:**
- Sets KUBECONFIG to `k8s/kubeconfig.yaml`
- Applies all Kubernetes manifests from `k8s/` directory

**When to use:**
- Manual deployment (not recommended in production)
- Local testing with k8s
- Emergency deployments

**Example:**
```bash
make deploy
```

**Requirements:**
- Valid `k8s/kubeconfig.yaml` file
- kubectl installed
- Access to cluster

**Note**: Automated deployment via GitHub Actions is preferred.

#### `make k8s-status`
**Purpose**: Check deployment status in Kubernetes

**What it does:**
- Shows pods, services, and ingress for this service
- Filters by app label

**When to use:**
- After deployment
- Troubleshooting deployment issues
- Monitoring service health

**Example:**
```bash
make k8s-status
```

#### `make k8s-logs`
**Purpose**: View Kubernetes pod logs

**What it does:**
- Shows last 50 lines of logs
- Filters by app label

**When to use:**
- Debugging deployment issues
- Monitoring application behavior
- Investigating errors

**Example:**
```bash
make k8s-logs
```

**Tip**: For more logs, use kubectl directly:
```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml logs -n petrosa-apps -l app=SERVICE_NAME --tail=100 -f
```

#### `make k8s-clean`
**Purpose**: Clean up Kubernetes resources

**What it does:**
- Deletes the `petrosa-apps` namespace

**When to use:**
- Development/testing cleanup
- Resetting environment
- Removing all service resources

**Example:**
```bash
make k8s-clean
```

**Warning**: This is destructive and will delete ALL resources in the namespace!

### Complete Pipeline

#### `make pipeline`
**Purpose**: Run complete CI/CD pipeline locally

**What it does:**
1. Clean up environment
2. Install dev dependencies
3. Format code
4. Run linting
5. Run type checking
6. Run tests with coverage
7. Run security scans
8. Build Docker image
9. Test container

**When to use:**
- Before committing major changes
- Before creating a PR
- To verify everything works end-to-end

**Example:**
```bash
make pipeline
```

**Duration**: Typically 2-5 minutes depending on test suite

**Exit codes:**
- 0: All stages passed
- 1: Any stage failed

## Common Workflows

### Starting Development
```bash
git clone <repository>
cd <repository>
make setup
```

### Before Committing
```bash
make format
make lint
make test
```

### Quick Check
```bash
make format lint test
```

### Full Validation
```bash
make pipeline
```

### Deployment Check
```bash
make build container
make k8s-status
```

## Environment Variables

The Makefile uses the following variables that can be overridden:

```bash
# Override coverage threshold
make test COVERAGE_THRESHOLD=60

# Override Python executable
make setup PYTHON=python3.11

# Override image name
make build IMAGE_NAME=custom-name

# Override namespace
make k8s-status NAMESPACE=my-namespace
```

## Makefile Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PYTHON` | `python3` | Python executable |
| `COVERAGE_THRESHOLD` | `40` | Minimum coverage percentage |
| `IMAGE_NAME` | `basename $(CURDIR)` | Docker image name |
| `NAMESPACE` | `petrosa-apps` | Kubernetes namespace |

## Customization

To customize the Makefile for your service:

1. **Coverage threshold**: Adjust `COVERAGE_THRESHOLD` variable
2. **Service-specific commands**: Add new targets at the end
3. **Test paths**: Modify test commands if tests are in different location
4. **Build args**: Add Docker build arguments to `build` target

**Example - Add custom target:**
```makefile
migrate: ## Run database migrations
	python manage.py migrate
```

## Troubleshooting

### Command Not Found

**Issue**: `make: command not found`

**Solution**: Install make
```bash
# macOS
xcode-select --install

# Ubuntu/Debian
sudo apt-get install build-essential

# Fedora/RHEL
sudo dnf install make
```

### Permission Denied

**Issue**: Permission errors when running commands

**Solution**: Ensure you have proper permissions
```bash
chmod +x scripts/*.sh  # Make scripts executable
```

### Docker Errors

**Issue**: Docker commands fail

**Solution**: Ensure Docker is running
```bash
docker info  # Check Docker status
```

### Kubernetes Connection Errors

**Issue**: kubectl cannot connect to cluster

**Solution**: Verify kubeconfig
```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml cluster-info
```

## Best Practices

1. **Run `make help`** to see all available commands
2. **Use `make pipeline`** before creating PRs
3. **Keep Makefile updated** when adding new tools
4. **Don't modify** the standardized Makefile structure
5. **Add service-specific targets** at the end of Makefile
6. **Document new targets** with `## comments`
7. **Use `.PHONY`** for all targets that don't create files

## Related Documentation

- [CI/CD Pipeline](./CI_CD_PIPELINE.md)
- [Testing Guide](./TESTING.md)
- [Quick Reference](./QUICK_REFERENCE.md)

# CI/CD Pipeline

## Overview

This service uses GitHub Actions for automated CI/CD with semantic versioning and automated deployment to Kubernetes. Every push to `main` triggers an automated deployment, while pull requests run comprehensive checks.

## Workflows

### CI Checks (`ci-checks.yml`)

Runs on every pull request to `main` or `develop`:

**Jobs:**
1. **Lint & Format Check**
   - Runs flake8 linting (critical errors + style issues)
   - Runs ruff linting with auto-fix
   - Runs mypy type checking (warnings only initially)

2. **Test & Coverage**
   - Runs pytest with coverage reporting
   - Enforces minimum 40% code coverage
   - Uploads coverage to Codecov
   - Fails CI if coverage is below threshold

3. **Test Quality Check** (BLOCKING)
   - Validates that all test functions contain assertions
   - Uses AST parsing to detect tests without assertions
   - **BLOCKS PRs** - CI will fail if tests without assertions are detected
   - Provides clear error messages indicating which tests need assertions
   - **NO continue-on-error** - This is a strict quality gate
   - Override process documented in `docs/TEST_QUALITY_CHECK_INTEGRATION.md`

4. **Security Scan**
   - Runs Trivy filesystem vulnerability scanner
   - Scans for HIGH and CRITICAL vulnerabilities
   - Reports findings in SARIF format

**When it runs:**
- On every PR to `main` or `develop` branches
- On every commit pushed to an open PR

**Failure behavior:**
- PR cannot be merged if any job fails
- Coverage below 40% will fail the build
- Codecov upload failures will fail the build
- **Tests without assertions will BLOCK the PR** (test quality gate is blocking)

### Deployment (`deploy.yml`)

Runs automatically on push to `main`:

**Jobs:**
1. **Create Release**
   - Auto-increments semantic version (e.g., v1.0.0 → v1.0.1)
   - Creates and pushes git tag
   - Uses patch version increment for every deployment

2. **Build & Push**
   - Builds Docker image with multi-platform support
   - Tags image with version and `latest`
   - Pushes to Docker Hub registry
   - Uses GitHub Actions cache for faster builds

3. **Deploy to Kubernetes**
   - Connects to remote MicroK8s cluster
   - Replaces VERSION_PLACEHOLDER in manifests
   - Applies Kubernetes manifests
   - Waits for deployment to be ready
   - Verifies pods, services, and ingress

4. **Notify**
   - Reports deployment status
   - Includes version and image information

5. **Cleanup**
   - Cleans up old Docker images (if configured)

**When it runs:**
- On every push to `main` branch (typically after PR merge)
- Only runs if CI checks have passed

**Environment:**
- Deployment uses `production` environment
- Requires approval if configured in GitHub settings

## Running Locally

### Full Pipeline
```bash
make pipeline  # Runs all CI/CD stages locally
```

### Individual Stages
```bash
make setup      # Setup environment
make format            # Format code
make lint              # Run linting
make type-check        # Run type checking
make test              # Run tests with coverage
make test-quality-check # Check test assertions (BLOCKING quality gate)
make security          # Run security scans
make build             # Build Docker image
make container         # Test container
```

### Quick Checks Before PR
```bash
make format lint test
```

## Coverage Requirements

- **Minimum**: 40%
- **Enforced**: Yes (CI will fail below threshold)
- **Tracked**: Codecov (https://codecov.io/gh/PetroSa2/REPO_NAME)
- **Reports**: Generated in `htmlcov/` directory locally

**How to check coverage locally:**
```bash
make coverage          # Run tests with coverage
open htmlcov/index.html  # View HTML report
```

## Semantic Versioning

The service uses automated semantic versioning:

- **Format**: `vMAJOR.MINOR.PATCH` (e.g., v1.2.3)
- **Auto-increment**: Patch version incremented on every main push
- **Manual bump**: Create a tag manually for major/minor changes
- **Tag location**: Git tags, visible in GitHub releases

**Version determination:**
1. Gets latest tag from git (e.g., v1.0.5)
2. Increments patch version (→ v1.0.6)
3. If no tags exist, starts at v1.0.0

## Secrets Required

The following secrets must be configured in GitHub repository settings:

1. **CODECOV_TOKEN**: Token for uploading coverage to Codecov
2. **DOCKERHUB_USERNAME**: Docker Hub username
3. **DOCKERHUB_TOKEN**: Docker Hub access token
4. **KUBE_CONFIG_DATA**: Base64-encoded kubeconfig for MicroK8s cluster
5. **GITHUB_TOKEN**: Automatically provided by GitHub Actions

**To set secrets:**
1. Go to repository Settings → Secrets and variables → Actions
2. Click "New repository secret"
3. Add each secret with its value

## Deployment Process

### Automatic Deployment (Recommended)

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make changes and commit
3. Push and create PR: `git push origin feature/my-feature`
4. CI checks run automatically
5. Review and merge PR
6. Deployment to Kubernetes happens automatically
7. Verify deployment: `make k8s-status`

### Manual Deployment (Not Recommended)

```bash
# Build and push manually
docker build -t user/repo:tag .
docker push user/repo:tag

# Deploy manually
export KUBECONFIG=k8s/kubeconfig.yaml
kubectl apply -f k8s/
```

## Monitoring Deployments

### Check deployment status
```bash
make k8s-status
```

### View logs
```bash
make k8s-logs
```

### Check specific deployment
```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml get deployment DEPLOYMENT_NAME -n petrosa-apps
```

### Rollback if needed
```bash
kubectl --kubeconfig=k8s/kubeconfig.yaml rollout undo deployment/DEPLOYMENT_NAME -n petrosa-apps
```

## Troubleshooting

### CI Checks Failing

**Linting errors:**
```bash
make format  # Auto-fix formatting
make lint    # Check for remaining issues
```

**Test failures:**
```bash
make test -v  # Run with verbose output
pytest tests/path/to/test.py -v  # Run specific test
```

**Coverage too low:**
```bash
make coverage  # See coverage report
open htmlcov/index.html  # View detailed HTML report
# Add more tests to increase coverage
```

### Deployment Failing

**Image not found:**
- Check Docker Hub for image
- Verify DOCKERHUB_USERNAME and DOCKERHUB_TOKEN secrets
- Check build-and-push job logs

**Kubernetes connection issues:**
- Verify KUBE_CONFIG_DATA secret is correct
- Check if cluster is accessible
- Verify namespace exists: `kubectl get namespace petrosa-apps`

**VERSION_PLACEHOLDER not replaced:**
- This is expected - replacement happens in CI/CD
- Never manually replace VERSION_PLACEHOLDER in manifests
- Check deploy job logs for replacement step

## Best Practices

1. **Always create PRs**: Never push directly to `main`
2. **Run local checks first**: `make pipeline` before pushing
3. **Keep PRs small**: Easier to review and test
4. **Write tests**: Maintain 40%+ coverage
5. **Fix CI immediately**: Don't merge failing PRs
6. **Monitor deployments**: Check logs after deployment
7. **Use semantic commits**: Follow conventional commits format

## Related Documentation

- [Testing Guide](./TESTING.md)
- [Makefile Reference](./MAKEFILE.md)
- [Repository Setup Guide](./REPOSITORY_SETUP_GUIDE.md)
- [Quick Reference](./QUICK_REFERENCE.md)

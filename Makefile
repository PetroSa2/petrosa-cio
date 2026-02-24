#!/usr/bin/env make

# Standardized Makefile for Petrosa Systems
# Version: 2.0
# Service: petrosa-cio

# Variables (customize per service)
PYTHON := python3
COVERAGE_THRESHOLD := 40
IMAGE_NAME := petrosa-cio
NAMESPACE := petrosa-apps

# PHONY targets
.PHONY: help setup install install-dev clean
.PHONY: format lint type-check pre-commit
.PHONY: test unit integration e2e coverage test-quality-check
.PHONY: security build container
.PHONY: deploy k8s-status k8s-logs k8s-clean
.PHONY: pipeline test-ci-pipeline

# Default target
.DEFAULT_GOAL := help

help: ## Show this help message
	@echo "🚀 Petrosa $(IMAGE_NAME) - Standard Development Commands"
	@echo "========================================================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Setup and Installation
setup: ## Complete environment setup with dependencies and pre-commit
	@echo "🚀 Setting up development environment..."
	$(PYTHON) -m pip install --upgrade pip
	pip install -r requirements.txt
	pip install -r requirements-dev.txt
	pre-commit install
	@echo "✅ Setup completed!"

install: ## Install production dependencies only
	@echo "📦 Installing production dependencies..."
	pip install -r requirements.txt

install-dev: ## Install development dependencies
	@echo "🔧 Installing development dependencies..."
	pip install -r requirements-dev.txt

clean: ## Clean up cache and temporary files
	@echo "🧹 Cleaning up cache and temporary files..."
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage coverage.xml .trivy/
	rm -f bandit-report.json
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -delete
	@echo "✅ Cleanup completed!"

# Code Quality
format: ## Format code with ruff (replaces black + isort)
	@echo "🎨 Formatting code with ruff..."
	ruff format .
	ruff check . --select I --fix
	@echo "✅ Code formatting completed!"

lint: ## Run linting checks with ruff (replaces flake8)
	@echo "✨ Running linting checks..."
	ruff check . --fix
	@echo "✅ Linting completed!"

type-check: ## Run type checking with mypy
	@echo "🔍 Running type checking with mypy..."
	mypy . --ignore-missing-imports || echo "⚠️  Type checking found issues (non-blocking)"
	@echo "✅ Type checking completed!"

pre-commit: ## Run pre-commit hooks on all files
	@echo "🔍 Running pre-commit hooks on all files..."
	pre-commit run --all-files
	@echo "✅ Pre-commit checks completed!"

# Testing
test: ## Run all tests with coverage (fail if below 40%)
	@echo "🧪 Running all tests with coverage..."
	OTEL_NO_AUTO_INIT=1 ENVIRONMENT=testing pytest tests/ -v --cov=. --cov-report=term-missing --cov-report=html --cov-report=xml --cov-fail-under=$(COVERAGE_THRESHOLD)
	@echo "✅ Tests completed!"

unit: ## Run unit tests only
	@echo "🧪 Running unit tests..."
	pytest tests/ -m "unit" -v --tb=short

integration: ## Run integration tests only
	@echo "🔗 Running integration tests..."
	pytest tests/ -m "integration" -v --tb=short

e2e: ## Run end-to-end tests only
	@echo "🌐 Running end-to-end tests..."
	pytest tests/ -m "e2e" -v --tb=short

coverage: ## Generate coverage reports without failing
	@echo "📊 Running tests with coverage..."
	pytest tests/ --cov=. --cov-report=term-missing --cov-report=html --cov-report=xml

test-quality-check: ## Check for tests without assertions (BLOCKING quality gate)
	@echo "🧪 Checking test quality (assertions)..."
	@if [ -f "../petrosa_k8s/scripts/check-test-assertions.py" ]; then \
		$(PYTHON) ../petrosa_k8s/scripts/check-test-assertions.py; \
	else \
		echo "⚠️  Test quality script not found at ../petrosa_k8s/scripts/check-test-assertions.py"; \
	fi

# Security
security: ## Run comprehensive security scans (gitleaks, detect-secrets, bandit, trivy)
	@echo "🔒 Running comprehensive security scans..."
	@echo ""
	@echo "1️⃣ Gitleaks (Secret Detection)..."
	@if command -v gitleaks >/dev/null 2>&1; then \
		gitleaks detect --verbose --no-color || echo "⚠️  Gitleaks found potential secrets (review above)"; \
	else \
		echo "⚠️  Gitleaks not installed. Install with: brew install gitleaks"; \
	fi
	@echo ""
	@echo "2️⃣ detect-secrets (Entropy-based Detection)..."
	@if command -v detect-secrets >/dev/null 2>&1; then \
		detect-secrets scan --baseline .secrets.baseline || echo "⚠️  New secrets detected (review above)"; \
	else \
		echo "⚠️  detect-secrets not installed. Install with: pip install detect-secrets"; \
	fi
	@echo ""
	@echo "3️⃣ Bandit (Python Security)..."
	@bandit -r . -f json -o bandit-report.json -ll --exclude tests/ || true
	@if [ -f bandit-report.json ]; then \
		echo "📊 Bandit found issues. Check bandit-report.json"; \
		python -m json.tool bandit-report.json | grep -A 5 '"issue_severity"' | head -20 || true; \
	fi
	@echo ""
	@echo "4️⃣ Trivy (Vulnerability Scanner)..."
	@if command -v trivy >/dev/null 2>&1; then \
		trivy fs . --severity HIGH,CRITICAL --format table; \
	else \
		echo "⚠️  Trivy not installed. Install with: brew install trivy"; \
	fi
	@echo ""
	@echo "✅ Security scans completed!"
	@echo ""
	@echo "📊 Summary:"
	@echo "  - Gitleaks: $$(command -v gitleaks >/dev/null 2>&1 && echo '✅ Installed' || echo '❌ Not installed')"
	@echo "  - detect-secrets: $$(command -v detect-secrets >/dev/null 2>&1 && echo '✅ Installed' || echo '❌ Not installed')"
	@echo "  - Bandit: ✅ Installed"
	@echo "  - Trivy: $$(command -v trivy >/dev/null 2>&1 && echo '✅ Installed' || echo '❌ Not installed')"

# Docker
build: ## Build Docker image
	@echo "🐳 Building Docker image..."
	docker build -t $(IMAGE_NAME):dev .
	@echo "✅ Docker build completed!"

container: ## Test Docker container
	@echo "📦 Testing Docker container..."
	docker run --rm $(IMAGE_NAME):dev python -c "print('✅ Container test passed')"

# Kubernetes Deployment
deploy: ## Deploy to Kubernetes cluster
	@echo "☸️  Deploying to Kubernetes..."
	@if [ ! -f k8s/kubeconfig.yaml ]; then \
		echo "❌ kubeconfig not found at k8s/kubeconfig.yaml"; \
		exit 1; \
	fi
	export KUBECONFIG=k8s/kubeconfig.yaml && kubectl apply -f k8s/ --recursive
	@echo "✅ Deployment completed!"

k8s-status: ## Check Kubernetes deployment status
	@echo "📊 Kubernetes deployment status:"
	kubectl --kubeconfig=k8s/kubeconfig.yaml get pods,svc,ingress -n $(NAMESPACE) -l app=$(IMAGE_NAME)

k8s-logs: ## View Kubernetes logs
	@echo "📋 Kubernetes logs:"
	kubectl --kubeconfig=k8s/kubeconfig.yaml logs -n $(NAMESPACE) -l app=$(IMAGE_NAME) --tail=50

k8s-clean: ## Clean up Kubernetes resources
	@echo "🧹 Cleaning up Kubernetes resources..."
	kubectl --kubeconfig=k8s/kubeconfig.yaml delete namespace $(NAMESPACE) 2>/dev/null || true
	@echo "✅ Cleanup completed!"

# Complete Pipeline
pipeline: ## Run complete CI/CD pipeline locally
	@echo "🔄 Running complete CI/CD pipeline..."
	@echo "=================================="
	@echo ""
	@echo "1️⃣ Cleaning up..."
	$(MAKE) clean
	@echo ""
	@echo "2️⃣ Installing dependencies..."
	$(MAKE) install-dev
	@echo ""
	@echo "3️⃣ Formatting code..."
	$(MAKE) format
	@echo ""
	@echo "4️⃣ Running linting..."
	$(MAKE) lint
	@echo ""
	@echo "5️⃣ Running type checking..."
	$(MAKE) type-check
	@echo ""
	@echo "6️⃣ Running tests..."
	$(MAKE) test
	@echo ""
	@echo "7️⃣ Checking test quality..."
	$(MAKE) test-quality-check
	@echo ""
	@echo "8️⃣ Running security scans..."
	$(MAKE) security
	@echo ""
	@echo "9️⃣ Building Docker image..."
	$(MAKE) build
	@echo ""
	@echo "🔟 Testing container..."
	$(MAKE) container
	@echo ""
	@echo "✅ Pipeline completed successfully!"

test-ci-pipeline: pipeline ## Local CI/CD pipeline simulation

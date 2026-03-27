#!/usr/bin/env make

# Standardized Makefile for Petrosa Systems
# Version: 2.0
# Service: petrosa-cio

# Python enforcement
PYTHON_VERSION_EXPECTED := 3.11
PYTHON_VERSION_ACTUAL := $(shell python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)

# Colors for output
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[0;33m
BLUE := \033[0;34m
NC := \033[0m # No Color

# Variables
PYTHON := python3
COVERAGE_THRESHOLD := 40
IMAGE_NAME := petrosa-cio
NAMESPACE := petrosa-apps
RUFF := $(if $(wildcard ./venv/bin/ruff),./venv/bin/ruff,ruff)

# PHONY targets
.PHONY: help setup install install-dev clean
.PHONY: format lint type-check
.PHONY: test unit integration coverage
.PHONY: security build container
.PHONY: pipeline

.PHONY: pipeline validate-python

# Default target
.DEFAULT_GOAL := help

help: ## Show this help message
	@echo "$(BLUE)Petrosa $(IMAGE_NAME) - Standard Development Commands$(NC)"
	@echo "$(BLUE)========================================================$(NC)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

validate-python: ## Validate Python version is 3.11
	@echo "$(BLUE)Validating Python version...$(NC)"
	@if [ "$(PYTHON_VERSION_ACTUAL)" != "$(PYTHON_VERSION_EXPECTED)" ]; then \
		echo "$(RED)❌ ERROR: Python $(PYTHON_VERSION_EXPECTED) required, found $(PYTHON_VERSION_ACTUAL)$(NC)"; \
		echo "$(YELLOW)💡 Recommended resolution: Use 'pyenv install 3.11.9 && pyenv local 3.11.9'$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✅ Python version $(PYTHON_VERSION_ACTUAL) matches expected $(PYTHON_VERSION_EXPECTED)$(NC)"

# Setup and Installation
setup: validate-python ## Complete environment setup with dependencies
	@echo "$(BLUE)Setting up development environment...$(NC)"
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -r requirements-dev.txt
	@echo "✅ Setup completed!"

install: validate-python ## Install production dependencies only
	@echo "📦 Installing production dependencies..."
	$(PYTHON) -m pip install -r requirements.txt

install-dev: ## Install development dependencies
	@echo "🔧 Installing development dependencies..."
	$(PYTHON) -m pip install -r requirements-dev.txt

clean: ## Clean up cache and temporary files
	@echo "🧹 Cleaning up cache and temporary files..."
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage coverage.xml
	rm -f bandit-report.json
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	@echo "✅ Cleanup completed!"

# Code Quality
format: ## Format code with ruff
	@echo "🎨 Formatting code with ruff..."
	$(RUFF) format .
	$(RUFF) check . --select I --fix
	@echo "✅ Code formatting completed!"

lint: ## Run linting checks with ruff
	@echo "✨ Running linting checks..."
	$(RUFF) check . --fix
	@echo "✅ Linting completed!"

type-check: ## Run type checking with mypy
	@echo "🔍 Running type checking with mypy..."
	$(PYTHON) -m mypy . --ignore-missing-imports || echo "⚠️  Type checking found issues (non-blocking)"
	@echo "✅ Type checking completed!"

# Testing
test: validate-python ## Run all tests with coverage
	@echo "$(BLUE)🧪 Running all tests with coverage...$(NC)"
	ENVIRONMENT=testing PYTHONPATH=. pytest tests/ -v --cov=cio --cov-report=term-missing --cov-fail-under=$(COVERAGE_THRESHOLD)
	@echo "✅ Tests completed!"

test-coverage: test ## Alias for test (standardized)

test-quality: validate-python ## Run test quality check (assertions check)
	@echo "🔍 Checking test quality..."
	python3 scripts/check-test-assertions.py $(shell find tests -name "test_*.py")

unit: ## Run unit tests only
	@echo "🧪 Running unit tests..."
	PYTHONPATH=. pytest tests/unit/ -v

integration: ## Run integration tests only
	@echo "🔗 Running integration tests..."
	PYTHONPATH=. pytest tests/integration/ -v

# Security
security: ## Run security scans (Bandit)
	@echo "🔒 Running security scans..."
	$(PYTHON) -m bandit -r cio/ -f json -o bandit-report.json || echo "⚠️  Bandit found issues"
	@echo "✅ Security scans completed!"

# Docker
build: ## Build Docker image
	@echo "🐳 Building Docker image..."
	docker build -t $(IMAGE_NAME):latest .

# Complete Pipeline
pipeline: validate-python ## Run complete CI/CD pipeline locally
	@echo "$(BLUE)🔄 Running complete CI/CD pipeline...$(NC)"
	$(MAKE) clean
	$(MAKE) format
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) security
	@echo "✅ Pipeline completed successfully!"

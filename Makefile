#!/usr/bin/env make

# Standardized Makefile for Petrosa Systems
# Version: 2.0
# Service: petrosa-cio

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

# Default target
.DEFAULT_GOAL := help

help: ## Show this help message
	@echo "🚀 Petrosa $(IMAGE_NAME) - Standard Development Commands"
	@echo "========================================================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Setup and Installation
setup: ## Complete environment setup with dependencies
	@echo "🚀 Setting up development environment..."
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -r requirements-dev.txt
	@echo "✅ Setup completed!"

install: ## Install production dependencies only
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
test: ## Run all tests with coverage
	@echo "🧪 Running all tests with coverage..."
	ENVIRONMENT=testing PYTHONPATH=. pytest tests/ -v --cov=cio --cov-report=term-missing --cov-fail-under=$(COVERAGE_THRESHOLD)
	@echo "✅ Tests completed!"

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
pipeline: ## Run complete CI/CD pipeline locally
	@echo "🔄 Running complete CI/CD pipeline..."
	$(MAKE) clean
	$(MAKE) format
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) security
	@echo "✅ Pipeline completed successfully!"

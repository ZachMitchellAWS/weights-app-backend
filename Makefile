# Python AWS CDK Microservices Bootstrap - Makefile
#
# Quick Start:
#   make venv               - Create virtual environment (recommended)
#   source venv/bin/activate - Activate virtual environment
#   make install            - Install all dependencies
#   make bootstrap-staging  - Bootstrap CDK for staging (first time only)
#   make deploy-staging     - Deploy to staging environment
#   make test               - Run unit tests
#
# Common Workflow:
#   1. make venv && source venv/bin/activate
#   2. make install
#   3. make bootstrap-staging (first time only)
#   4. make synth-staging (optional - preview templates)
#   5. make deploy-staging
#   6. make test

.PHONY: help venv install install-dev build-layer upload-email-templates-staging upload-email-templates-production bootstrap-staging bootstrap-production synth-staging synth-production diff-staging diff-production deploy-staging deploy-production destroy-staging destroy-production clear-staging-db save-user-staging load-user-staging test lint format clean

# Python command - use python3 for macOS/Linux compatibility
PYTHON := python3
PIP := $(PYTHON) -m pip

# Default target
help:
	@echo "Available commands:"
	@echo "  make venv                 - Create virtual environment (recommended)"
	@echo "  make install              - Install CDK dependencies"
	@echo "  make install-dev          - Install development dependencies"
	@echo "  make build-layer          - Build Lambda layer with dependencies"
	@echo "  make upload-email-templates-staging    - Upload email templates to staging S3 bucket"
	@echo "  make upload-email-templates-production - Upload email templates to production S3 bucket"
	@echo "  make bootstrap-staging    - Bootstrap CDK for staging (first time only)"
	@echo "  make bootstrap-production - Bootstrap CDK for production (first time only)"
	@echo "  make synth-staging        - Synthesize staging CloudFormation templates"
	@echo "  make synth-production     - Synthesize production CloudFormation templates"
	@echo "  make diff-staging         - Show staging changes compared to deployed stack"
	@echo "  make diff-production      - Show production changes compared to deployed stack"
	@echo "  make deploy-staging       - Deploy to staging environment"
	@echo "  make deploy-production    - Deploy to production (with confirmation)"
	@echo "  make destroy-staging      - Destroy staging resources"
	@echo "  make destroy-production   - Destroy production resources (with confirmation)"
	@echo "  make clear-staging-db     - Delete all items from staging DynamoDB tables"
	@echo "  make save-user-staging    - Save test user data to snapshot file"
	@echo "  make load-user-staging    - Load test user data from snapshot file"
	@echo "  make test                 - Run unit tests"
	@echo "  make lint                 - Run linting (flake8, mypy)"
	@echo "  make format               - Format code with black"
	@echo "  make clean                - Remove generated files and caches"

# Create virtual environment
venv:
	$(PYTHON) -m venv venv
	@echo ""
	@echo "Virtual environment created. Activate it with:"
	@echo "  source venv/bin/activate"

# Install CDK dependencies
install:
	$(PIP) install -r requirements.txt

# Install development dependencies
install-dev:
	$(PIP) install -r requirements-dev.txt

# Build Lambda layer with dependencies (PyJWT, pydantic)
# No Docker needed - all pure Python packages
build-layer:
	@echo "Building Lambda layer for auth service..."
	@echo "Installing pure Python dependencies (PyJWT, pydantic)..."
	cd services/auth && rm -rf layer/python && mkdir -p layer/python
	cd services/auth && pip3 install -r requirements.txt -t layer/python/ --upgrade
	@echo "Lambda layer built successfully at services/auth/layer/"

# Upload email templates to staging S3 bucket
upload-email-templates-staging:
	@echo "Uploading email templates to staging S3 bucket..."
	aws s3 sync email-templates/ s3://project-staging-email-templates/ \
		--exclude "*" \
		--include "*.html" \
		--region us-west-1
	@echo "Email templates uploaded successfully to project-staging-email-templates"

# Upload email templates to production S3 bucket
upload-email-templates-production:
	@echo "Uploading email templates to production S3 bucket..."
	aws s3 sync email-templates/ s3://project-production-email-templates/ \
		--exclude "*" \
		--include "*.html" \
		--region us-west-1
	@echo "Email templates uploaded successfully to project-production-email-templates"

# Bootstrap CDK for staging environment
bootstrap-staging:
	cdk bootstrap aws://569134947863/us-west-1 -c env=staging

# Bootstrap CDK for production environment
bootstrap-production:
	cdk bootstrap aws://569134947863/us-west-1 -c env=production

# Synthesize staging CloudFormation templates
synth-staging:
	cdk synth -c env=staging

# Synthesize production CloudFormation templates
synth-production:
	cdk synth -c env=production

# Show staging changes
diff-staging:
	cdk diff -c env=staging --all

# Show production changes
diff-production:
	cdk diff -c env=production --all

# Deploy to staging
deploy-staging:
	cdk deploy -c env=staging --all --require-approval never

# Deploy to production (with confirmation)
deploy-production:
	@echo "WARNING: Deploying to PRODUCTION environment!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	cdk deploy -c env=production --all

# Destroy staging resources
destroy-staging:
	cdk destroy -c env=staging --all

# Destroy production resources (with confirmation)
destroy-production:
	@echo "WARNING: Destroying PRODUCTION resources!"
	@echo "This action cannot be undone. Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	cdk destroy -c env=production --all

# Clear all items from staging DynamoDB tables
# WARNING: This deletes ALL data from staging tables
clear-staging-db:
	@echo "WARNING: This will delete ALL items from staging DynamoDB tables!"
	@echo "Tables: users, user-properties, password-reset-codes, exercises, lift-sets, estimated-1rm"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	$(PYTHON) scripts/clear_staging_db.py

# Save test user data from staging DynamoDB to snapshot file
# User ID is hardcoded in the script (18dee8ea-ac11-4b02-ae52-670cb830e44a)
save-user-staging:
	@echo "Saving test user data from staging DynamoDB tables..."
	$(PYTHON) scripts/user_snapshot.py save

# Load test user data from snapshot file back into staging DynamoDB
# User ID is hardcoded in the script (18dee8ea-ac11-4b02-ae52-670cb830e44a)
load-user-staging:
	@echo "Loading test user data into staging DynamoDB tables..."
	$(PYTHON) scripts/user_snapshot.py load

# Run unit tests
test:
	pytest services/auth/tests/unit -v --cov=services/auth/lambda --cov-report=term-missing

# Run linting
lint:
	flake8 services/ --max-line-length=120 --exclude=__pycache__,*.pyc,cdk.out
	mypy services/ --ignore-missing-imports

# Format code
format:
	black services/ app.py config/ --line-length=120

# Clean generated files
clean:
	rm -rf cdk.out/
	rm -rf cdk.context.json
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type f -name ".coverage" -delete
	find . -type d -name "htmlcov" -exec rm -rf {} +

# Clean everything including virtual environment
clean-all: clean
	rm -rf venv/

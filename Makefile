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

.PHONY: help venv install install-dev build-layer upload-email-templates-staging upload-email-templates-production bootstrap-staging bootstrap-production bootstrap-us-east-1 synth-staging synth-production diff-staging diff-production deploy-staging deploy-production destroy-staging destroy-production clear-staging-db clear-production-db save-user-staging load-user-staging load-power-user-staging deploy-website-cert-staging deploy-website-cert-production deploy-website-infra-staging deploy-website-infra-production deploy-website-staging deploy-website-production whitelist-ip-staging test lint format clean

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
	@echo "  make clear-production-db  - Delete all items from production DynamoDB tables"
	@echo "  make save-user-staging    - Save test user data to snapshot file"
	@echo "  make load-user-staging    - Load test user data from snapshot file"
	@echo "  make bootstrap-us-east-1              - Bootstrap CDK for us-east-1 (website cert, first time)"
	@echo "  make deploy-website-cert-staging       - Deploy website cert stack (us-east-1, first time)"
	@echo "  make deploy-website-cert-production    - Deploy website cert stack (us-east-1, first time)"
	@echo "  make deploy-website-infra-staging      - Deploy website infra (S3, CloudFront, Lambda, etc.)"
	@echo "  make deploy-website-infra-production   - Deploy website infra to production"
	@echo "  make deploy-website-staging            - Build React + sync to S3 + invalidate CloudFront"
	@echo "  make deploy-website-production         - Build React + sync to S3 + invalidate (production)"
	@echo "  make whitelist-ip-staging              - Whitelist your current IP on staging WAF"
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

# Build Lambda layer with dependencies (PyJWT, pydantic, cryptography)
# Uses --platform to get Linux-compatible wheels for Lambda (Amazon Linux 2)
build-layer:
	@echo "Building Lambda layer for auth service..."
	cd services/auth && rm -rf layer/python && mkdir -p layer/python
	cd services/auth && pip3 install -r requirements.txt -t layer/python/ --upgrade --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all:
	@echo "Lambda layer built successfully at services/auth/layer/"
	@echo ""
	@echo "Building Lambda layer for entitlements service..."
	cd services/entitlements && rm -rf layer/python && mkdir -p layer/python
	cd services/entitlements && pip3 install -r requirements.txt -t layer/python/ --upgrade --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all:
	@echo "Lambda layer built successfully at services/entitlements/layer/"
	@echo ""
	@echo "Building Lambda layer for insights service..."
	cd services/insights && rm -rf layer/python && mkdir -p layer/python
	cd services/insights && pip3 install -r requirements.txt -t layer/python/ --upgrade --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all:
	@echo "Lambda layer built successfully at services/insights/layer/"
	@echo ""
	@echo "Building Lambda layer for user service..."
	cd services/user && rm -rf layer/python && mkdir -p layer/python
	cd services/user && pip3 install -r requirements.txt -t layer/python/ --upgrade --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all:
	@echo "Lambda layer built successfully at services/user/layer/"
	@echo ""
	@echo "Building Lambda layer for checkin service..."
	cd services/checkin && rm -rf layer/python && mkdir -p layer/python
	cd services/checkin && pip3 install -r requirements.txt -t layer/python/ --upgrade --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all:
	@echo "Lambda layer built successfully at services/checkin/layer/"
	@echo ""
	@echo "Building Lambda layer for email service..."
	cd services/email && rm -rf layer/python && mkdir -p layer/python
	cd services/email && pip3 install -r requirements.txt -t layer/python/ --upgrade --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all:
	@echo "Lambda layer built successfully at services/email/layer/"
	@echo ""
	@echo "Building Lambda layer for website service..."
	cd services/website && rm -rf layer/python && mkdir -p layer/python
	cd services/website && pip3 install -r requirements.txt -t layer/python/ --upgrade --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all:
	@echo "Lambda layer built successfully at services/website/layer/"

# Upload email templates to staging S3 bucket
upload-email-templates-staging:
	@echo "Uploading email templates to staging S3 bucket..."
	aws s3 sync email-templates/ s3://liftthebull-staging-email-templates/ \
		--exclude "*" \
		--include "*.html" \
		--region us-west-1
	@echo "Email templates uploaded successfully to liftthebull-staging-email-templates"

# Upload email templates to production S3 bucket
upload-email-templates-production:
	@echo "Uploading email templates to production S3 bucket..."
	aws s3 sync email-templates/ s3://liftthebull-production-email-templates/ \
		--exclude "*" \
		--include "*.html" \
		--region us-west-1
	@echo "Email templates uploaded successfully to liftthebull-production-email-templates"

# Set CloudWatch log retention for staging (30 days)
set-log-retention-staging:
	@echo "Setting log retention to 30 days for staging..."
	@for fn in auth checkin user entitlements insights email-processing website-support; do \
		echo "  /aws/lambda/liftthebull-staging-$$fn"; \
		aws logs put-retention-policy \
			--log-group-name /aws/lambda/liftthebull-staging-$$fn \
			--retention-in-days 30 \
			--region us-west-1 2>/dev/null || echo "    (log group not found, skipping)"; \
	done
	@echo "Done"

# Set CloudWatch log retention for production (90 days)
set-log-retention-production:
	@echo "Setting log retention to 90 days for production..."
	@for fn in auth checkin user entitlements insights email-processing website-support; do \
		echo "  /aws/lambda/liftthebull-production-$$fn"; \
		aws logs put-retention-policy \
			--log-group-name /aws/lambda/liftthebull-production-$$fn \
			--retention-in-days 90 \
			--region us-west-1 2>/dev/null || echo "    (log group not found, skipping)"; \
	done
	@echo "Done"

# Bootstrap CDK for staging environment
bootstrap-staging:
	cdk bootstrap aws://569134947863/us-west-1 -c env=staging

# Bootstrap CDK for production environment
bootstrap-production:
	cdk bootstrap aws://569134947863/us-west-1 -c env=production

# Bootstrap CDK for us-east-1 (required for website cert stack, first time only)
bootstrap-us-east-1:
	cdk bootstrap aws://569134947863/us-east-1

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
	@echo "Tables: users, user-properties, password-reset-codes, exercises, lift-sets, estimated-1rm, splits, set-plans, accessory-goal-checkins, entitlement-grants, subscription-events, insight-tasks, insights-cache"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	$(PYTHON) scripts/clear_staging_db.py

# Clear all items from production DynamoDB tables
# WARNING: This deletes ALL data from production tables
clear-production-db:
	@echo "WARNING: This will delete ALL items from PRODUCTION DynamoDB tables!"
	@echo "Tables: users, user-properties, password-reset-codes, exercises, lift-sets, estimated-1rm, splits, set-plans, accessory-goal-checkins, entitlement-grants, subscription-events, insight-tasks, insights-cache"
	@echo "This action cannot be undone. Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	$(PYTHON) scripts/clear_production_db.py


# Save test user data from staging DynamoDB to snapshot file
# Optionally pass USER_ID: make save-user-staging USER_ID=1702dad4-...
save-user-staging:
	@echo "Saving test user data from staging DynamoDB tables..."
	$(PYTHON) scripts/user_snapshot.py save $(if $(USER_ID),--user-id $(USER_ID))

# Load test user data from snapshot file back into staging DynamoDB
# Optionally pass USER_ID: make load-user-staging USER_ID=1702dad4-...
load-user-staging:
	@echo "Loading test user data into staging DynamoDB tables..."
	$(PYTHON) scripts/user_snapshot.py load $(if $(USER_ID),--user-id $(USER_ID))

# Generate and load power user data (large dataset) into staging DynamoDB
# MONTHS defaults to 12 but can be overridden: make load-power-user-staging MONTHS=6
load-power-user-staging:
	@echo "Loading power user data into staging DynamoDB ($(or $(MONTHS),12) months)..."
	$(PYTHON) scripts/generate_power_user.py --months $(or $(MONTHS),12)

# Deploy website ACM certificate stack to staging (us-east-1, first time only)
# After deploy, copy the cert ARN to config/staging.py CLOUDFRONT_CERT_ARN
deploy-website-cert-staging:
	cdk deploy liftthebull-staging-website-cert -c env=staging --require-approval never

# Deploy website ACM certificate stack to production (us-east-1, first time only)
# After deploy, copy the cert ARN to config/production.py CLOUDFRONT_CERT_ARN
deploy-website-cert-production:
	@echo "WARNING: Deploying website cert to PRODUCTION (us-east-1)!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	cdk deploy liftthebull-production-website-cert -c env=production

# Deploy website infrastructure stack to staging (S3, CloudFront, DynamoDB, Lambda, API route)
deploy-website-infra-staging:
	cdk deploy liftthebull-staging-website -c env=staging --require-approval never

# Deploy website infrastructure stack to production
deploy-website-infra-production:
	@echo "WARNING: Deploying website infra to PRODUCTION!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	cdk deploy liftthebull-production-website -c env=production

# Build and deploy website to staging
# Builds React app, syncs to S3, invalidates CloudFront cache
deploy-website-staging:
	@echo "Building website for staging..."
	cd ./website && npm run build -- --mode staging
	@echo "Syncing to S3..."
	aws s3 sync ./website/dist/ s3://liftthebull-staging-website/ --delete --region us-west-1
	@echo "Setting AASA content-type..."
	aws s3 cp \
		s3://liftthebull-staging-website/.well-known/apple-app-site-association \
		s3://liftthebull-staging-website/.well-known/apple-app-site-association \
		--content-type application/json --metadata-directive REPLACE --region us-west-1
	@echo "Invalidating CloudFront cache..."
	aws cloudfront create-invalidation \
		--distribution-id $$(aws cloudformation describe-stacks \
			--stack-name liftthebull-staging-website \
			--region us-west-1 \
			--query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" \
			--output text) \
		--paths "/*"
	@echo "Website deployed to staging!"

# Build and deploy website to production
deploy-website-production:
	@echo "WARNING: Deploying website to PRODUCTION!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	@echo "Building website for production..."
	cd ./website && npm run build -- --mode production
	@echo "Syncing to S3..."
	aws s3 sync ./website/dist/ s3://liftthebull-production-website/ --delete --region us-west-1
	@echo "Setting AASA content-type..."
	aws s3 cp \
		s3://liftthebull-production-website/.well-known/apple-app-site-association \
		s3://liftthebull-production-website/.well-known/apple-app-site-association \
		--content-type application/json --metadata-directive REPLACE --region us-west-1
	@echo "Invalidating CloudFront cache..."
	aws cloudfront create-invalidation \
		--distribution-id $$(aws cloudformation describe-stacks \
			--stack-name liftthebull-production-website \
			--region us-west-1 \
			--query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" \
			--output text) \
		--paths "/*"
	@echo "Website deployed to production!"

# Whitelist your current public IP on the staging WAF
# Only replaces the IP set — no CDK deploy needed
whitelist-ip-staging:
	$(eval MY_IP := $(shell curl -s https://checkip.amazonaws.com))
	@echo "Whitelisting IP: $(MY_IP)/32"
	$(eval IP_SET_ID := $(shell aws cloudformation describe-stacks \
		--stack-name liftthebull-staging-website-cert \
		--region us-east-1 \
		--query "Stacks[0].Outputs[?OutputKey=='WafIpSetId'].OutputValue" \
		--output text))
	$(eval LOCK_TOKEN := $(shell aws wafv2 get-ip-set \
		--name liftthebull-staging-website-whitelist \
		--scope CLOUDFRONT \
		--id $(IP_SET_ID) \
		--region us-east-1 \
		--query "LockToken" --output text))
	aws wafv2 update-ip-set \
		--name liftthebull-staging-website-whitelist \
		--scope CLOUDFRONT \
		--id $(IP_SET_ID) \
		--lock-token $(LOCK_TOKEN) \
		--addresses "$(MY_IP)/32" \
		--region us-east-1
	@echo "Done! $(MY_IP) is now whitelisted on staging."

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

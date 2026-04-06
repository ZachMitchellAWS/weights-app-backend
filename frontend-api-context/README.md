# Project CDK API - Integration Reference

## Documentation Structure

This directory contains API documentation split by service:

| File | Description |
|------|-------------|
| [general.md](./general.md) | Environment config, headers, authentication, error handling |
| [auth.md](./auth.md) | Auth Service - user registration, login, tokens, password reset |
| [user.md](./user.md) | User Service - user properties management |
| [checkin.md](./checkin.md) | Checkin Service - exercises, lift sets, estimated 1RM |
| [entitlements.md](./entitlements.md) | Entitlements Service - subscription management via Apple |

## Quick Start

1. Read [general.md](./general.md) for environment setup and authentication
2. Use Auth Service to create account or login
3. Include `x-api-key` and `Authorization: Bearer <token>` headers in requests
4. Refer to individual service docs for endpoint details

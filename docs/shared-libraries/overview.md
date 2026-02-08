# Shared Libraries

Reusable, enterprise-grade libraries for secure application development across Python, Go, and Node.js/TypeScript ecosystems. These libraries implement consistent patterns for validation, security, cryptography, and more.

## Overview

The shared libraries provide:

- **Validation**: PyDAL-style input validators with chainable API
- **Security**: Rate limiting, CSRF protection, secure headers, sanitization
- **Crypto**: Token generation, password hashing, encryption
- **HTTP**: Request correlation, resilient HTTP client with retries
- **gRPC**: Server/client setup with security interceptors

## Available Libraries

### [Python Library](./py-libs.md)

```bash
pip install penguin-libs
```

Additional Python packages:
```bash
pip install penguin-licensing  # License validation
pip install penguin-sal        # Secrets abstraction layer
```

Full-featured Python library with Flask integration, gRPC support, and Redis connectivity.

**Key Features:**
- PyDAL-style validators with type hints
- Flask security middleware
- Argon2id password hashing
- AES-256-GCM encryption
- gRPC server with interceptors

### [Go Library](./go-libs.md)

```bash
go get github.com/penguintechinc/penguin-libs/packages/go-common
```

High-performance Go library for microservices with Gin framework integration.

**Key Features:**
- Chainable validators with functional options
- Gin middleware for security
- bcrypt password hashing
- XChaCha20-Poly1305 encryption
- gRPC server setup

### [Node.js/TypeScript Library](./node-libs.md)

```bash
npm install @penguintechinc/react-libs
```

Modern React/TypeScript library with full type safety (subsumes the former node_libs).

**Key Features:**
- Type-safe validators with chainable API
- Express middleware integration
- Argon2 password hashing
- ChaCha20-Poly1305 encryption
- gRPC client/server support

## Quick Start

### Python

```python
from py_libs.validation import chain, IsNotEmpty, IsLength, IsEmail

# Validate email with multiple validators
validators = chain(IsNotEmpty(), IsLength(3, 255), IsEmail())
result = validators("user@example.com")

if result.is_valid:
    email = result.unwrap()
else:
    print(f"Validation error: {result.error}")
```

### Go

```go
package main

import (
    "fmt"
    "github.com/penguintechinc/penguin-libs/packages/go-common/validation"
)

func main() {
    // Chain multiple validators
    validator := validation.Chain(
        validation.NotEmpty(),
        validation.Length(3, 255),
        validation.Email(),
    )

    result := validator.Validate("user@example.com")
    if !result.IsValid {
        fmt.Println("Error:", result.Error)
        return
    }

    email := result.Value.(string)
    fmt.Println("Valid email:", email)
}
```

### Node.js/TypeScript

```typescript
import { chain, notEmpty, length, email } from '@penguintechinc/react-libs/validation';

const validator = chain(
  notEmpty(),
  length(3, 255),
  email()
);

const result = validator("user@example.com");
if (!result.isValid) {
  console.error("Validation error:", result.error);
} else {
  console.log("Valid email:", result.value);
}
```

## Published Packages

These libraries are published from the [`penguin-libs`](https://github.com/penguintechinc/penguin-libs) monorepo. Local copies are no longer maintained in this repository.

| Package | Registry | Install |
|---------|----------|---------|
| `penguin-libs` | PyPI | `pip install penguin-libs` |
| `penguin-licensing` | PyPI | `pip install penguin-licensing` |
| `penguin-sal` | PyPI | `pip install penguin-sal` |
| `penguintechinc-utils` | PyPI | `pip install penguintechinc-utils` |
| `@penguintechinc/react-libs` | GitHub Packages (npm) | `npm install @penguintechinc/react-libs` |
| `go-common` | Go modules | `go get github.com/penguintechinc/penguin-libs/packages/go-common` |

## Common Features Across All Libraries

### Validation

All three libraries implement PyDAL-style validators with chainable APIs:

- String: empty, length, pattern matching, alphanumeric, slug, set membership
- Numeric: integer, float, range validation
- Network: email, URL, IP address, hostname
- DateTime: date, time, datetime, date range
- Password: configurable strength validation

### Security

Cross-library security utilities:

- Rate limiting (in-memory and Redis-backed)
- CSRF protection
- Secure HTTP headers
- Input sanitization
- Audit logging

### Cryptography

Standard cryptographic operations:

- **Password Hashing**: Argon2id (Python/Node.js), bcrypt (Go)
- **Encryption**: AES-256-GCM (Python), XChaCha20-Poly1305 (Go), ChaCha20-Poly1305 (Node.js)
- **Token Generation**: Secure random tokens
- **JWT**: JWT creation and validation

### gRPC

Production-ready gRPC support:

- Server setup with interceptors
- Security interceptors for auth/validation
- Error handling and logging
- Metadata propagation

## Development

Library development is done in the [`penguin-libs`](https://github.com/penguintechinc/penguin-libs) monorepo. See that repository for contributing guidelines, testing, and release workflows.

## Installation in Projects

### Using Python Libraries

```bash
# In requirements.txt
penguin-libs>=0.1.0
penguin-licensing>=0.1.0
penguin-sal>=0.1.0
```

### Using Go Library

```bash
go get github.com/penguintechinc/penguin-libs/packages/go-common
```

### Using React/Node.js Library

Requires `.npmrc` with GitHub Packages auth:
```
@penguintechinc:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
```

Then install:
```bash
npm install @penguintechinc/react-libs
```

## API Documentation

- **Python**: [py_libs README](./py-libs.md)
- **Go**: [go_libs README](./go-libs.md)
- **Node.js**: [node_libs README](./node-libs.md)

## Testing

Each published library includes comprehensive tests maintained in the `penguin-libs` monorepo:

- Unit tests for all validators and utilities
- Integration tests for middleware and frameworks
- Type checking (mypy for Python, strict TypeScript)
- Security scanning (bandit for Python, golangci-lint for Go)

## Security

All libraries are designed with security-first principles:

- Input validation to prevent injection attacks
- Password hashing with modern algorithms
- Secure randomization for tokens
- Protected against common vulnerabilities
- Regular security audits via scanning tools
- No hardcoded secrets or credentials

## License

Licensed under GNU Affero General Public License v3 (AGPL-3.0)

See LICENSE files in individual library directories for details.

---

For library-specific documentation, setup instructions, and API references, see the README files in each library directory.

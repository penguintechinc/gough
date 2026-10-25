# syntax=docker/dockerfile:1
# WebUI Dockerfile
# Node.js backend + React frontend shell
#
# @penguintechinc/* packages are consumed from the public npm registry (registry.npmjs.org),
# pinned exactly in package.json and locked with integrity hashes in package-lock.json.
# No registry credentials are required at build time.

# Build stage
FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS builder

WORKDIR /app

# Install from the committed lockfile
COPY package*.json ./
RUN npm ci

# Copy source code
COPY . .

# Build the application
RUN npm run build

# Production stage
FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS production

LABEL org.opencontainers.image.source="https://github.com/penguintechinc/gough"

WORKDIR /app

# Create non-root user
RUN groupadd -r webui && useradd -r -g webui webui

# Install production dependencies from the committed lockfile
COPY package*.json ./
RUN npm ci --omit=dev && npm cache clean --force

# Copy built assets from builder
COPY --from=builder /app/dist ./dist

# Runtime needs only node; drop the bundled npm/corepack toolchain (and its transitive
# deps, e.g. pacote/sigstore) so their advisories never ship in the production image.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack

# Set ownership
RUN chown -R webui:webui /app

# Switch to non-root user
USER webui

# Environment defaults
ENV NODE_ENV=production \
    PORT=3000 \
    FLASK_API_URL=http://flask-backend:5000 \
    GO_API_URL=http://go-backend:8080

# Expose port
EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3000/healthz || exit 1

# Run the server
CMD ["node", "dist/server/index.js"]

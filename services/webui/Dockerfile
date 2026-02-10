# WebUI Dockerfile
# Node.js backend + React frontend shell

# Build stage
FROM node:22-bookworm-slim AS builder

WORKDIR /app

# Copy react-libs from local source (until published to GitHub Packages)
COPY --from=penguin-libs packages/react-libs /tmp/react-libs

# Copy package files
COPY package*.json ./

# Rewrite @penguintechinc/react-libs to local file reference and install
# Remove this block once the package is published to GitHub Packages
RUN sed -i 's|"@penguintechinc/react-libs": "[^"]*"|"@penguintechinc/react-libs": "file:/tmp/react-libs"|' package.json && \
    rm -f package-lock.json && \
    npm install

# Copy source code
COPY . .

# Build the application
RUN npm run build

# Production stage
FROM node:22-bookworm-slim AS production

WORKDIR /app

# Create non-root user
RUN groupadd -r webui && useradd -r -g webui webui

# Copy react-libs from local source (until published to GitHub Packages)
COPY --from=penguin-libs packages/react-libs /tmp/react-libs

# Copy package files for production dependencies
COPY package*.json ./

# Rewrite @penguintechinc/react-libs to local file reference and install
# Remove this block once the package is published to GitHub Packages
RUN sed -i 's|"@penguintechinc/react-libs": "[^"]*"|"@penguintechinc/react-libs": "file:/tmp/react-libs"|' package.json && \
    rm -f package-lock.json && \
    npm install --omit=dev && npm cache clean --force && \
    rm -rf /tmp/react-libs

# Copy built assets from builder
COPY --from=builder /app/dist ./dist

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

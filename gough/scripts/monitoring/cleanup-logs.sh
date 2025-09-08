#!/bin/bash
# Gough Hypervisor - Log Cleanup Script
# Manages log retention and cleanup for monitoring and logging infrastructure

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default configuration
RETENTION_DAYS=30
ELASTICSEARCH_HOST="localhost:9200"
DRY_RUN=false
VERBOSE=false
FORCE=false

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_verbose() {
    if [[ "$VERBOSE" == "true" ]]; then
        echo -e "${BLUE}[VERBOSE]${NC} $1"
    fi
}

# Function to show usage
show_usage() {
    cat << EOF
Gough Hypervisor - Log Cleanup Script

Usage: $0 [OPTIONS]

OPTIONS:
    -r, --retention-days DAYS    Log retention period in days (default: 30)
    -e, --elasticsearch HOST     Elasticsearch host:port (default: localhost:9200)
    -d, --dry-run               Show what would be deleted without executing
    -v, --verbose               Enable verbose output
    -f, --force                 Force deletion without confirmation
    -h, --help                  Show this help message

EXAMPLES:
    $0                          # Clean logs older than 30 days
    $0 --retention-days 7       # Clean logs older than 7 days
    $0 --dry-run                # Preview what would be cleaned
    $0 --force                  # Skip confirmation prompts

EOF
}

# Function to check Elasticsearch connectivity
check_elasticsearch() {
    print_status "Checking Elasticsearch connectivity..."
    
    if curl -s -f "http://${ELASTICSEARCH_HOST}/_cluster/health" > /dev/null; then
        print_success "Connected to Elasticsearch at $ELASTICSEARCH_HOST"
        return 0
    else
        print_error "Cannot connect to Elasticsearch at $ELASTICSEARCH_HOST"
        return 1
    fi
}

# Function to get Elasticsearch indices
get_indices() {
    local pattern="$1"
    curl -s "http://${ELASTICSEARCH_HOST}/_cat/indices/${pattern}?h=index,creation.date.string" | sort -k2
}

# Function to clean Elasticsearch indices
clean_elasticsearch_indices() {
    print_status "Cleaning Elasticsearch indices older than $RETENTION_DAYS days..."
    
    local cutoff_date=$(date -d "$RETENTION_DAYS days ago" '+%Y.%m.%d')
    local indices_to_delete=""
    local deleted_count=0
    local total_size=0
    
    # Check gough-logs indices
    print_verbose "Checking gough-logs-* indices..."
    while IFS=$'\t' read -r index creation_date; do
        if [[ -n "$index" && -n "$creation_date" ]]; then
            local index_date=$(echo "$creation_date" | cut -d'T' -f1 | tr '-' '.')
            
            if [[ "$index_date" < "$cutoff_date" ]]; then
                indices_to_delete="$indices_to_delete $index"
                
                # Get index size
                local size=$(curl -s "http://${ELASTICSEARCH_HOST}/_cat/indices/$index?h=store.size&bytes=b" | tr -d ' ')
                if [[ "$size" =~ ^[0-9]+$ ]]; then
                    total_size=$((total_size + size))
                fi
                
                print_verbose "Index $index ($creation_date) marked for deletion"
            fi
        fi
    done < <(get_indices "gough-logs-*")
    
    # Check gough-errors indices
    print_verbose "Checking gough-errors-* indices..."
    while IFS=$'\t' read -r index creation_date; do
        if [[ -n "$index" && -n "$creation_date" ]]; then
            local index_date=$(echo "$creation_date" | cut -d'T' -f1 | tr '-' '.')
            
            if [[ "$index_date" < "$cutoff_date" ]]; then
                indices_to_delete="$indices_to_delete $index"
                
                local size=$(curl -s "http://${ELASTICSEARCH_HOST}/_cat/indices/$index?h=store.size&bytes=b" | tr -d ' ')
                if [[ "$size" =~ ^[0-9]+$ ]]; then
                    total_size=$((total_size + size))
                fi
                
                print_verbose "Index $index ($creation_date) marked for deletion"
            fi
        fi
    done < <(get_indices "gough-errors-*")
    
    # Check gough-security indices
    print_verbose "Checking gough-security-* indices..."
    while IFS=$'\t' read -r index creation_date; do
        if [[ -n "$index" && -n "$creation_date" ]]; then
            local index_date=$(echo "$creation_date" | cut -d'T' -f1 | tr '-' '.')
            
            if [[ "$index_date" < "$cutoff_date" ]]; then
                indices_to_delete="$indices_to_delete $index"
                
                local size=$(curl -s "http://${ELASTICSEARCH_HOST}/_cat/indices/$index?h=store.size&bytes=b" | tr -d ' ')
                if [[ "$size" =~ ^[0-9]+$ ]]; then
                    total_size=$((total_size + size))
                fi
                
                print_verbose "Index $index ($creation_date) marked for deletion"
            fi
        fi
    done < <(get_indices "gough-security-*")
    
    if [[ -z "$indices_to_delete" ]]; then
        print_success "No indices found older than $RETENTION_DAYS days"
        return 0
    fi
    
    deleted_count=$(echo "$indices_to_delete" | wc -w)
    local size_mb=$((total_size / 1024 / 1024))
    
    print_warning "Found $deleted_count indices to delete (${size_mb}MB total)"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        print_status "DRY RUN: Would delete the following indices:"
        for index in $indices_to_delete; do
            echo "  - $index"
        done
        return 0
    fi
    
    # Confirmation prompt
    if [[ "$FORCE" != "true" ]]; then
        echo -n "Are you sure you want to delete $deleted_count indices? [y/N] "
        read -r confirmation
        if [[ "$confirmation" != "y" && "$confirmation" != "Y" ]]; then
            print_status "Operation cancelled"
            return 0
        fi
    fi
    
    # Delete indices
    for index in $indices_to_delete; do
        print_status "Deleting index: $index"
        if curl -s -X DELETE "http://${ELASTICSEARCH_HOST}/$index" > /dev/null; then
            print_success "Deleted index: $index"
        else
            print_error "Failed to delete index: $index"
        fi
    done
    
    print_success "Deleted $deleted_count indices, freed ${size_mb}MB"
}

# Function to clean Docker container logs
clean_docker_logs() {
    print_status "Cleaning Docker container logs..."
    
    local containers=$(docker ps -q)
    local cleaned_count=0
    
    if [[ -z "$containers" ]]; then
        print_warning "No running containers found"
        return 0
    fi
    
    for container in $containers; do
        local container_name=$(docker inspect --format='{{.Name}}' "$container" | sed 's/^\//')
        local log_file="/var/lib/docker/containers/$container/$container-json.log"
        
        if [[ -f "$log_file" ]]; then
            local log_size=$(stat -f%z "$log_file" 2>/dev/null || stat -c%s "$log_file" 2>/dev/null || echo "0")
            local size_mb=$((log_size / 1024 / 1024))
            
            if [[ "$size_mb" -gt 100 ]]; then
                print_verbose "Container $container_name has ${size_mb}MB of logs"
                
                if [[ "$DRY_RUN" != "true" ]]; then
                    # Truncate the log file
                    echo "" > "$log_file" 2>/dev/null || true
                    print_success "Truncated logs for container: $container_name"
                    cleaned_count=$((cleaned_count + 1))
                fi
            fi
        fi
    done
    
    if [[ "$DRY_RUN" == "true" ]]; then
        print_status "DRY RUN: Would truncate logs for containers with >100MB logs"
    else
        print_success "Cleaned logs for $cleaned_count containers"
    fi
}

# Function to clean Prometheus data
clean_prometheus_data() {
    print_status "Cleaning old Prometheus data..."
    
    # Prometheus manages its own retention via configuration
    # We can only provide information about current data size
    
    local prometheus_data_dir="/var/lib/docker/volumes/gough_prometheus_data/_data"
    
    if [[ -d "$prometheus_data_dir" ]]; then
        local data_size=$(du -sh "$prometheus_data_dir" 2>/dev/null | cut -f1)
        print_status "Prometheus data size: $data_size"
        print_status "Prometheus manages its own data retention via configuration"
    else
        print_warning "Prometheus data directory not found"
    fi
}

# Function to clean system logs
clean_system_logs() {
    print_status "Cleaning system logs..."
    
    local cleaned_files=0
    local log_dirs=("/var/log" "/opt/gough/logs")
    
    for log_dir in "${log_dirs[@]}"; do
        if [[ ! -d "$log_dir" ]]; then
            continue
        fi
        
        # Find old log files
        local old_logs=$(find "$log_dir" -name "*.log" -type f -mtime +"$RETENTION_DAYS" 2>/dev/null || true)
        local old_gz_logs=$(find "$log_dir" -name "*.log.gz" -type f -mtime +"$RETENTION_DAYS" 2>/dev/null || true)
        
        if [[ -n "$old_logs" ]]; then
            if [[ "$DRY_RUN" == "true" ]]; then
                print_status "DRY RUN: Would delete old log files in $log_dir"
                echo "$old_logs" | while read -r log_file; do
                    print_verbose "Would delete: $log_file"
                done
            else
                echo "$old_logs" | while read -r log_file; do
                    rm -f "$log_file"
                    print_verbose "Deleted: $log_file"
                    cleaned_files=$((cleaned_files + 1))
                done
            fi
        fi
        
        if [[ -n "$old_gz_logs" ]]; then
            if [[ "$DRY_RUN" == "true" ]]; then
                print_status "DRY RUN: Would delete old compressed log files in $log_dir"
                echo "$old_gz_logs" | while read -r log_file; do
                    print_verbose "Would delete: $log_file"
                done
            else
                echo "$old_gz_logs" | while read -r log_file; do
                    rm -f "$log_file"
                    print_verbose "Deleted: $log_file"
                    cleaned_files=$((cleaned_files + 1))
                done
            fi
        fi
    done
    
    if [[ "$DRY_RUN" != "true" ]]; then
        print_success "Cleaned $cleaned_files system log files"
    fi
}

# Function to generate cleanup summary
generate_summary() {
    print_status "==================== CLEANUP SUMMARY ===================="
    print_status "Retention period: $RETENTION_DAYS days"
    print_status "Elasticsearch host: $ELASTICSEARCH_HOST"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        print_warning "DRY RUN MODE - No actual cleanup performed"
    fi
    
    # Show current disk usage
    print_status "Current disk usage:"
    df -h | head -1
    df -h / | tail -1
    
    print_status "Cleanup completed at $(date)"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -r|--retention-days)
            RETENTION_DAYS="$2"
            shift 2
            ;;
        -e|--elasticsearch)
            ELASTICSEARCH_HOST="$2"
            shift 2
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Validate retention days
if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || [[ "$RETENTION_DAYS" -lt 1 ]]; then
    print_error "Invalid retention days: $RETENTION_DAYS"
    exit 1
fi

# Main execution
print_status "Starting Gough log cleanup..."
print_status "Retention period: $RETENTION_DAYS days"

if [[ "$DRY_RUN" == "true" ]]; then
    print_warning "Running in DRY RUN mode - no actual cleanup will be performed"
fi

# Check Elasticsearch connectivity
if check_elasticsearch; then
    clean_elasticsearch_indices
else
    print_warning "Skipping Elasticsearch cleanup due to connectivity issues"
fi

# Clean Docker logs
clean_docker_logs

# Clean Prometheus data (informational)
clean_prometheus_data

# Clean system logs
clean_system_logs

# Generate summary
generate_summary

print_success "Log cleanup completed successfully!"
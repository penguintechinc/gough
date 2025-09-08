/**
 * MaaS Infrastructure Automation Portal
 * Main JavaScript Application
 */

// Global application object
window.MaaSPortal = {
    // Configuration
    config: {
        apiBaseUrl: '/api',
        refreshInterval: 30000,
        chartColors: {
            primary: '#0d6efd',
            success: '#198754',
            warning: '#ffc107',
            danger: '#dc3545',
            info: '#0dcaf0',
            secondary: '#6c757d'
        }
    },
    
    // State management
    state: {
        statusRefreshTimer: null,
        activeCharts: {}
    },
    
    // Initialize application
    init: function() {
        this.setupStatusMonitoring();
        this.setupEventListeners();
        this.initializeTooltips();
        this.setupAutoRefresh();
        
        // Initialize page-specific functionality
        this.initializePageFeatures();
    },
    
    // Setup status monitoring
    setupStatusMonitoring: function() {
        this.updateSystemStatus();
        
        // Refresh status every 30 seconds
        this.state.statusRefreshTimer = setInterval(() => {
            this.updateSystemStatus();
        }, this.config.refreshInterval);
    },
    
    // Update system status indicator
    updateSystemStatus: function() {
        fetch(this.config.apiBaseUrl + '/status')
            .then(response => response.json())
            .then(data => {
                const statusIcon = document.getElementById('status-icon');
                const statusIndicator = document.getElementById('status-indicator');
                
                if (!statusIcon || !statusIndicator) return;
                
                // Update status icon
                statusIcon.className = 'bi bi-circle-fill';
                statusIndicator.title = 'System Status';
                
                if (data.status === 'ok') {
                    // Check component statuses
                    const components = data.components || {};
                    const hasErrors = Object.values(components).some(status => 
                        typeof status === 'string' && status.startsWith('error')
                    );
                    
                    if (hasErrors) {
                        statusIcon.className += ' text-warning';
                        statusIndicator.title = 'System Status: Some issues detected';
                    } else {
                        statusIcon.className += ' text-success';
                        statusIndicator.title = 'System Status: All systems operational';
                    }
                } else {
                    statusIcon.className += ' text-danger';
                    statusIndicator.title = 'System Status: Error - ' + (data.message || 'Unknown error');
                }
            })
            .catch(error => {
                console.error('Failed to update system status:', error);
                const statusIcon = document.getElementById('status-icon');
                if (statusIcon) {
                    statusIcon.className = 'bi bi-circle-fill text-danger';
                    document.getElementById('status-indicator').title = 'System Status: Connection error';
                }
            });
    },
    
    // Setup global event listeners
    setupEventListeners: function() {
        // Handle form submissions with loading states
        document.addEventListener('submit', (e) => {
            const form = e.target;
            if (form.classList.contains('ajax-form')) {
                e.preventDefault();
                this.handleAjaxForm(form);
            } else {
                this.showLoadingState(form);
            }
        });
        
        // Handle AJAX links
        document.addEventListener('click', (e) => {
            const link = e.target.closest('.ajax-link');
            if (link) {
                e.preventDefault();
                this.handleAjaxLink(link);
            }
        });
        
        // Handle refresh buttons
        document.addEventListener('click', (e) => {
            if (e.target.matches('[data-refresh]')) {
                e.preventDefault();
                const target = e.target.dataset.refresh;
                this.refreshSection(target);
            }
        });
    },
    
    // Initialize Bootstrap tooltips
    initializeTooltips: function() {
        const tooltipElements = document.querySelectorAll('[data-bs-toggle="tooltip"]');
        tooltipElements.forEach(element => {
            new bootstrap.Tooltip(element);
        });
    },
    
    // Setup auto-refresh for data sections
    setupAutoRefresh: function() {
        const refreshElements = document.querySelectorAll('[data-auto-refresh]');
        refreshElements.forEach(element => {
            const interval = parseInt(element.dataset.autoRefresh) * 1000 || 60000;
            const refreshTarget = element.dataset.refreshTarget || element.id;
            
            setInterval(() => {
                this.refreshSection(refreshTarget);
            }, interval);
        });
    },
    
    // Initialize page-specific features
    initializePageFeatures: function() {
        // Initialize charts if Chart.js is available
        if (typeof Chart !== 'undefined') {
            this.initializeCharts();
        }
        
        // Initialize log viewers
        this.initializeLogViewers();
        
        // Initialize server cards
        this.initializeServerCards();
        
        // Initialize deployment timeline
        this.initializeDeploymentTimeline();
    },
    
    // Initialize charts
    initializeCharts: function() {
        // Status distribution chart
        const statusChartCanvas = document.getElementById('statusChart');
        if (statusChartCanvas && window.statusChartData) {
            this.createStatusChart(statusChartCanvas, window.statusChartData);
        }
        
        // Fleet status chart
        const fleetChartCanvas = document.getElementById('fleetChart');
        if (fleetChartCanvas && window.fleetChartData) {
            this.createFleetChart(fleetChartCanvas, window.fleetChartData);
        }
        
        // Performance charts
        document.querySelectorAll('[data-chart-type]').forEach(canvas => {
            this.initializeChart(canvas);
        });
    },
    
    // Create status distribution chart
    createStatusChart: function(canvas, data) {
        const ctx = canvas.getContext('2d');
        
        this.state.activeCharts.statusChart = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: data.labels,
                datasets: [{
                    data: data.data,
                    backgroundColor: [
                        this.config.chartColors.success,
                        this.config.chartColors.warning,
                        this.config.chartColors.danger,
                        this.config.chartColors.info,
                        this.config.chartColors.secondary
                    ],
                    borderWidth: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            padding: 20,
                            usePointStyle: true
                        }
                    }
                },
                cutout: '60%'
            }
        });
    },
    
    // Create fleet status chart
    createFleetChart: function(canvas, data) {
        const ctx = canvas.getContext('2d');
        
        this.state.activeCharts.fleetChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['Online', 'Offline', 'New', 'MIA'],
                datasets: [{
                    label: 'Hosts',
                    data: [data.online || 0, data.offline || 0, data.new || 0, data.mia || 0],
                    backgroundColor: [
                        this.config.chartColors.success,
                        this.config.chartColors.danger,
                        this.config.chartColors.info,
                        this.config.chartColors.warning
                    ],
                    borderRadius: 4,
                    borderSkipped: false
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: false
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: {
                            stepSize: 1
                        }
                    }
                }
            }
        });
    },
    
    // Initialize generic chart
    initializeChart: function(canvas) {
        const chartType = canvas.dataset.chartType;
        const chartData = canvas.dataset.chartData;
        
        if (!chartData) return;
        
        try {
            const data = JSON.parse(chartData);
            const ctx = canvas.getContext('2d');
            
            // Basic chart configuration
            const config = {
                type: chartType,
                data: data,
                options: {
                    responsive: true,
                    maintainAspectRatio: false
                }
            };
            
            this.state.activeCharts[canvas.id] = new Chart(ctx, config);
        } catch (error) {
            console.error('Failed to initialize chart:', error);
        }
    },
    
    // Initialize log viewers
    initializeLogViewers: function() {
        document.querySelectorAll('.log-viewer').forEach(viewer => {
            // Auto-scroll to bottom
            viewer.scrollTop = viewer.scrollHeight;
            
            // Add real-time updates if configured
            if (viewer.dataset.autoUpdate) {
                this.setupLogAutoUpdate(viewer);
            }
        });
    },
    
    // Setup log auto-update
    setupLogAutoUpdate: function(viewer) {
        const interval = parseInt(viewer.dataset.autoUpdate) * 1000 || 5000;
        const endpoint = viewer.dataset.endpoint;
        
        if (!endpoint) return;
        
        setInterval(() => {
            this.updateLogViewer(viewer, endpoint);
        }, interval);
    },
    
    // Update log viewer content
    updateLogViewer: function(viewer, endpoint) {
        const lastTimestamp = viewer.dataset.lastTimestamp;
        const url = lastTimestamp ? `${endpoint}?since=${lastTimestamp}` : endpoint;
        
        fetch(url)
            .then(response => response.json())
            .then(data => {
                if (data.logs && data.logs.length > 0) {
                    data.logs.forEach(log => {
                        this.appendLogEntry(viewer, log);
                    });
                    
                    // Update last timestamp
                    if (data.latest_timestamp) {
                        viewer.dataset.lastTimestamp = data.latest_timestamp;
                    }
                    
                    // Auto-scroll to bottom
                    viewer.scrollTop = viewer.scrollHeight;
                }
            })
            .catch(error => {
                console.error('Failed to update log viewer:', error);
            });
    },
    
    // Append log entry to viewer
    appendLogEntry: function(viewer, log) {
        const entry = document.createElement('div');
        entry.className = `log-entry log-${log.level.toLowerCase()}`;
        entry.innerHTML = `
            <span class="log-timestamp">${log.timestamp}</span>
            <span class="log-level">[${log.level}]</span>
            <span class="log-message">${this.escapeHtml(log.message)}</span>
        `;
        
        viewer.appendChild(entry);
        
        // Limit number of entries to prevent memory issues
        const maxEntries = parseInt(viewer.dataset.maxEntries) || 500;
        const entries = viewer.querySelectorAll('.log-entry');
        if (entries.length > maxEntries) {
            entries[0].remove();
        }
    },
    
    // Initialize server cards
    initializeServerCards: function() {
        document.querySelectorAll('.server-card').forEach(card => {
            // Add hover effects
            card.addEventListener('mouseenter', () => {
                card.style.transform = 'translateY(-2px)';
            });
            
            card.addEventListener('mouseleave', () => {
                card.style.transform = 'translateY(0)';
            });
        });
    },
    
    // Initialize deployment timeline
    initializeDeploymentTimeline: function() {
        const timeline = document.querySelector('.deployment-timeline');
        if (!timeline) return;
        
        // Add animation effects
        const items = timeline.querySelectorAll('.timeline-item');
        items.forEach((item, index) => {
            item.style.animationDelay = `${index * 0.1}s`;
            item.classList.add('animate-slide-in');
        });
    },
    
    // Handle AJAX form submissions
    handleAjaxForm: function(form) {
        const formData = new FormData(form);
        const url = form.action || window.location.href;
        const method = form.method || 'POST';
        
        this.showLoadingState(form);
        
        fetch(url, {
            method: method,
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            this.hideLoadingState(form);
            
            if (data.success) {
                this.showMessage('success', data.message || 'Operation completed successfully');
                
                // Refresh related sections
                if (data.refresh) {
                    data.refresh.forEach(section => {
                        this.refreshSection(section);
                    });
                }
                
                // Redirect if specified
                if (data.redirect) {
                    window.location.href = data.redirect;
                }
            } else {
                this.showMessage('danger', data.error || 'Operation failed');
            }
        })
        .catch(error => {
            this.hideLoadingState(form);
            this.showMessage('danger', 'Network error: ' + error.message);
        });
    },
    
    // Handle AJAX links
    handleAjaxLink: function(link) {
        const url = link.href;
        const target = link.dataset.target;
        
        if (!target) {
            window.location.href = url;
            return;
        }
        
        const targetElement = document.getElementById(target);
        if (!targetElement) {
            window.location.href = url;
            return;
        }
        
        this.showLoadingState(targetElement);
        
        fetch(url)
            .then(response => response.text())
            .then(html => {
                targetElement.innerHTML = html;
                this.hideLoadingState(targetElement);
                
                // Re-initialize features for new content
                this.initializePageFeatures();
            })
            .catch(error => {
                this.hideLoadingState(targetElement);
                this.showMessage('danger', 'Failed to load content: ' + error.message);
            });
    },
    
    // Refresh a section of the page
    refreshSection: function(sectionId) {
        const section = document.getElementById(sectionId);
        if (!section) return;
        
        const refreshUrl = section.dataset.refreshUrl || window.location.href;
        
        this.showLoadingState(section);
        
        fetch(refreshUrl)
            .then(response => response.text())
            .then(html => {
                // Extract content for the specific section
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                const newContent = doc.getElementById(sectionId);
                
                if (newContent) {
                    section.innerHTML = newContent.innerHTML;
                }
                
                this.hideLoadingState(section);
                this.initializePageFeatures();
            })
            .catch(error => {
                this.hideLoadingState(section);
                this.showMessage('danger', 'Failed to refresh section: ' + error.message);
            });
    },
    
    // Show loading state
    showLoadingState: function(element) {
        element.classList.add('loading');
    },
    
    // Hide loading state
    hideLoadingState: function(element) {
        element.classList.remove('loading');
    },
    
    // Show message to user
    showMessage: function(type, message) {
        const container = document.getElementById('flash-messages');
        if (!container) return;
        
        const alert = document.createElement('div');
        alert.className = `alert alert-${type} alert-dismissible fade show`;
        alert.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        
        container.appendChild(alert);
        
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            if (alert.parentNode) {
                alert.remove();
            }
        }, 5000);
    },
    
    // Utility: Escape HTML
    escapeHtml: function(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
    
    // Utility: Format bytes
    formatBytes: function(bytes, decimals = 2) {
        if (bytes === 0) return '0 Bytes';
        
        const k = 1024;
        const dm = decimals < 0 ? 0 : decimals;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        
        return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
    },
    
    // Utility: Format duration
    formatDuration: function(seconds) {
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const secs = seconds % 60;
        
        if (hours > 0) {
            return `${hours}h ${minutes}m ${secs}s`;
        } else if (minutes > 0) {
            return `${minutes}m ${secs}s`;
        } else {
            return `${secs}s`;
        }
    },
    
    // Cleanup function
    destroy: function() {
        // Clear timers
        if (this.state.statusRefreshTimer) {
            clearInterval(this.state.statusRefreshTimer);
        }
        
        // Destroy charts
        Object.values(this.state.activeCharts).forEach(chart => {
            if (chart && typeof chart.destroy === 'function') {
                chart.destroy();
            }
        });
        
        this.state.activeCharts = {};
    }
};

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    window.MaaSPortal.init();
});

// Cleanup when page is unloaded
window.addEventListener('beforeunload', function() {
    window.MaaSPortal.destroy();
});
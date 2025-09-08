# Detailed System Architecture Diagrams

This document provides comprehensive architectural diagrams and visual representations of the Gough hypervisor automation system.

## System Overview Diagram

```mermaid
graph TB
    subgraph "External Infrastructure"
        Internet[Internet/External Networks]
        PhysicalServers[Physical Servers<br/>To Be Provisioned]
        IPMI[IPMI/BMC<br/>Management]
    end
    
    subgraph "Gough Container Ecosystem"
        subgraph "Core Services Layer"
            MaaS[MaaS Container<br/>PXE Boot & Provisioning<br/>Port: 5240]
            Management[Management Server<br/>py4web Portal<br/>Port: 8000]
            FleetDM[FleetDM Container<br/>Security Monitoring<br/>Port: 8443]
        end
        
        subgraph "Data Layer"
            PostgreSQL[(PostgreSQL<br/>Management DB<br/>Port: 5432)]
            MySQL[(MySQL<br/>FleetDM DB<br/>Port: 3306)]
            Redis[(Redis Cache<br/>Sessions & Jobs<br/>Port: 6379)]
        end
        
        subgraph "Supporting Services"
            Nginx[Nginx Proxy<br/>Load Balancer<br/>Port: 80/443]
            Prometheus[Prometheus<br/>Metrics Collection<br/>Port: 9090]
            Grafana[Grafana<br/>Monitoring Dashboard<br/>Port: 3000]
            ELK[ELK Stack<br/>Log Aggregation<br/>Ports: 5601/9200]
        end
        
        subgraph "Network Services"
            DNS[DNS Server<br/>Port: 53]
            DHCP[DHCP Server<br/>Port: 67]
            TFTP[TFTP Server<br/>Port: 69]
        end
    end
    
    subgraph "Deployed Infrastructure"
        WebServers[Web Server Nodes<br/>Docker Hosts]
        DatabaseServers[Database Server Nodes<br/>PostgreSQL/MySQL]
        K8sNodes[Kubernetes Nodes<br/>Worker & Master]
        Agents[Gough Agents<br/>Monitoring & Management]
        OSQuery[OSQuery Agents<br/>Security Monitoring]
    end
    
    %% External connections
    Internet --> Nginx
    PhysicalServers -->|PXE Boot| DNS
    PhysicalServers -->|PXE Boot| DHCP
    PhysicalServers -->|PXE Boot| TFTP
    IPMI -->|Power Control| MaaS
    
    %% Internal container connections
    Nginx --> Management
    Nginx --> FleetDM
    Nginx --> MaaS
    Nginx --> Grafana
    
    Management --> PostgreSQL
    Management --> Redis
    Management --> MaaS
    Management --> FleetDM
    
    FleetDM --> MySQL
    FleetDM --> Redis
    
    MaaS --> DNS
    MaaS --> DHCP
    MaaS --> TFTP
    
    Prometheus --> Management
    Prometheus --> MaaS
    Prometheus --> FleetDM
    Grafana --> Prometheus
    
    ELK --> Management
    ELK --> MaaS
    ELK --> FleetDM
    
    %% Deployed infrastructure connections
    Agents --> Management
    OSQuery --> FleetDM
    WebServers --> Agents
    DatabaseServers --> Agents
    K8sNodes --> Agents
    
    style MaaS fill:#e1f5fe
    style Management fill:#f3e5f5
    style FleetDM fill:#fff3e0
    style PostgreSQL fill:#e8f5e8
    style MySQL fill:#e8f5e8
    style Redis fill:#ffebee
```

## Deployment Flow Diagram

```mermaid
sequenceDiagram
    participant Admin as Administrator
    participant Portal as Management Portal
    participant MaaS as MaaS Server
    participant Server as Physical Server
    participant Agent as Gough Agent
    participant Fleet as FleetDM
    
    Admin->>Portal: 1. Login & Select Template
    Portal->>MaaS: 2. Check Server Status
    MaaS-->>Portal: 3. Server Available
    Admin->>Portal: 4. Deploy Server Request
    Portal->>MaaS: 5. Generate Cloud-Init
    Portal->>MaaS: 6. Deploy Command
    MaaS->>Server: 7. PXE Boot Signal
    Server->>MaaS: 8. DHCP Request
    MaaS-->>Server: 9. IP Address & Boot Info
    Server->>MaaS: 10. TFTP Boot File Request
    MaaS-->>Server: 11. Ubuntu 24.04 Boot Files
    Server->>Server: 12. OS Installation
    Server->>Server: 13. Cloud-Init Execution
    Server->>Agent: 14. Agent Installation
    Agent->>Portal: 15. Agent Registration
    Agent->>Fleet: 16. OSQuery Enrollment
    Portal-->>Admin: 17. Deployment Complete
    
    Note over Server,Agent: Ongoing Monitoring
    Agent->>Portal: Health Updates
    Agent->>Fleet: Security Events
```

## Network Architecture Diagram

```mermaid
graph TB
    subgraph "Management Network (10.0.1.0/24)"
        AdminPC[Administrator<br/>Workstation<br/>10.0.1.10]
        ManagementVIP[Management VIP<br/>10.0.1.100]
        
        subgraph "Management Services"
            LB1[Load Balancer 1<br/>10.0.1.101]
            LB2[Load Balancer 2<br/>10.0.1.102]
            Gough1[Gough Node 1<br/>10.0.1.111]
            Gough2[Gough Node 2<br/>10.0.1.112]
            DB1[Database Primary<br/>10.0.1.121]
            DB2[Database Replica<br/>10.0.1.122]
        end
    end
    
    subgraph "Provisioning Network (192.168.1.0/24)"
        MaaSServer[MaaS Server<br/>192.168.1.10]
        DHCPRange[DHCP Pool<br/>192.168.1.100-200]
        
        subgraph "PXE Boot Infrastructure"
            DNSService[DNS Server<br/>192.168.1.10:53]
            DHCPService[DHCP Server<br/>192.168.1.10:67]
            TFTPService[TFTP Server<br/>192.168.1.10:69]
            HTTPService[HTTP Server<br/>192.168.1.10:80]
        end
        
        subgraph "Physical Servers"
            Server1[Server Node 1<br/>192.168.1.101]
            Server2[Server Node 2<br/>192.168.1.102]
            ServerN[Server Node N<br/>192.168.1.1XX]
        end
    end
    
    subgraph "Production Network (10.0.100.0/24)"
        ProdServers[Production Servers<br/>10.0.100.10-254]
        Services[Application Services<br/>Databases, Web, etc.]
    end
    
    subgraph "Monitoring Network (10.0.2.0/24)"
        MonitoringStack[Prometheus/Grafana<br/>10.0.2.10]
        LoggingStack[ELK Stack<br/>10.0.2.20]
        SecurityStack[FleetDM<br/>10.0.2.30]
    end
    
    Internet[Internet] --> ManagementVIP
    ManagementVIP --> LB1
    ManagementVIP --> LB2
    LB1 --> Gough1
    LB1 --> Gough2
    LB2 --> Gough1
    LB2 --> Gough2
    
    Gough1 --> DB1
    Gough2 --> DB1
    DB1 --> DB2
    
    Gough1 -.->|API Calls| MaaSServer
    Gough2 -.->|API Calls| MaaSServer
    
    MaaSServer --> DNSService
    MaaSServer --> DHCPService
    MaaSServer --> TFTPService
    MaaSServer --> HTTPService
    
    DHCPRange --> Server1
    DHCPRange --> Server2
    DHCPRange --> ServerN
    
    Server1 --> ProdServers
    Server2 --> ProdServers
    ServerN --> ProdServers
    
    ProdServers --> MonitoringStack
    ProdServers --> LoggingStack
    ProdServers --> SecurityStack
    
    style ManagementVIP fill:#ffcdd2
    style MaaSServer fill:#e1f5fe
    style MonitoringStack fill:#f3e5f5
    style LoggingStack fill:#fff3e0
    style SecurityStack fill:#e8f5e8
```

## Container Interaction Diagram

```mermaid
graph LR
    subgraph "Docker Host"
        subgraph "gough-network (172.20.0.0/16)"
            subgraph "Web Tier"
                Nginx[nginx-proxy<br/>172.20.0.2<br/>:80,:443]
            end
            
            subgraph "Application Tier"
                Management[management-server<br/>172.20.0.10<br/>:8000]
                MaaS[maas-server<br/>172.20.0.11<br/>:5240]
                Fleet[fleetdm-server<br/>172.20.0.12<br/>:8443]
                Workers[celery-workers<br/>172.20.0.13-15<br/>Background Jobs]
            end
            
            subgraph "Data Tier"
                PostgreSQL[postgresql<br/>172.20.0.20<br/>:5432]
                MySQL[mysql<br/>172.20.0.21<br/>:3306]
                Redis[redis<br/>172.20.0.22<br/>:6379]
            end
            
            subgraph "Monitoring Tier"
                Prometheus[prometheus<br/>172.20.0.30<br/>:9090]
                Grafana[grafana<br/>172.20.0.31<br/>:3000]
                AlertManager[alertmanager<br/>172.20.0.32<br/>:9093]
            end
            
            subgraph "Logging Tier"
                Elasticsearch[elasticsearch<br/>172.20.0.40<br/>:9200]
                Logstash[logstash<br/>172.20.0.41<br/>:5000]
                Kibana[kibana<br/>172.20.0.42<br/>:5601]
            end
        end
        
        subgraph "Host Network Services"
            DNS_Host[DNS:53<br/>Host Network]
            DHCP_Host[DHCP:67<br/>Host Network]
            TFTP_Host[TFTP:69<br/>Host Network]
        end
    end
    
    External[External Users<br/>& Systems] --> Nginx
    
    Nginx --> Management
    Nginx --> Fleet
    Nginx --> MaaS
    Nginx --> Grafana
    Nginx --> Kibana
    
    Management --> PostgreSQL
    Management --> Redis
    Management --> Workers
    Fleet --> MySQL
    Fleet --> Redis
    
    MaaS --> DNS_Host
    MaaS --> DHCP_Host
    MaaS --> TFTP_Host
    
    Workers --> PostgreSQL
    Workers --> Redis
    Workers --> MaaS
    Workers --> Fleet
    
    Prometheus --> Management
    Prometheus --> MaaS
    Prometheus --> Fleet
    Prometheus --> PostgreSQL
    Prometheus --> MySQL
    Prometheus --> Redis
    
    Grafana --> Prometheus
    AlertManager --> Prometheus
    
    Logstash --> Management
    Logstash --> MaaS
    Logstash --> Fleet
    Logstash --> Elasticsearch
    
    Kibana --> Elasticsearch
    
    style Nginx fill:#ffcdd2
    style Management fill:#e1f5fe
    style MaaS fill:#f3e5f5
    style Fleet fill:#fff3e0
```

## Security Architecture Diagram

```mermaid
graph TB
    subgraph "Security Perimeters"
        subgraph "DMZ (Demilitarized Zone)"
            WebProxy[Web Proxy/WAF<br/>Public Access Point]
            JumpHost[Jump Host/Bastion<br/>Secure Access]
        end
        
        subgraph "Management Zone"
            subgraph "Identity & Access"
                Auth[Authentication Service<br/>JWT/LDAP Integration]
                RBAC[Role-Based Access Control<br/>Permission Management]
                Vault[Secret Management<br/>API Keys & Certificates]
            end
            
            subgraph "Core Security"
                TLS[TLS Termination<br/>Certificate Management]
                APIGateway[API Gateway<br/>Rate Limiting & Filtering]
                AuditLog[Audit Logging<br/>Security Event Tracking]
            end
        end
        
        subgraph "Service Zone"
            subgraph "Application Security"
                AppSec[Application Firewall<br/>Input Validation]
                SQLSec[Database Security<br/>Encrypted Connections]
                ContainerSec[Container Security<br/>Runtime Protection]
            end
            
            subgraph "Network Security"
                NetSeg[Network Segmentation<br/>VLANs & Firewalls]
                Monitoring[Security Monitoring<br/>IDS/IPS]
                Encryption[Encryption at Rest<br/>& in Transit]
            end
        end
        
        subgraph "Data Zone"
            subgraph "Data Protection"
                DBEncryption[Database Encryption<br/>TDE & Column Level]
                BackupSec[Backup Security<br/>Encrypted Backups]
                DataGov[Data Governance<br/>Classification & Retention]
            end
        end
        
        subgraph "Monitoring Zone"
            subgraph "Security Operations"
                SIEM[SIEM Platform<br/>ELK Stack Integration]
                SOC[Security Operations<br/>Incident Response]
                Compliance[Compliance Monitoring<br/>Policy Enforcement]
            end
        end
    end
    
    Internet[Internet] --> WebProxy
    WebProxy --> JumpHost
    JumpHost --> Auth
    Auth --> RBAC
    RBAC --> Vault
    
    Vault --> TLS
    TLS --> APIGateway
    APIGateway --> AuditLog
    
    AuditLog --> AppSec
    AppSec --> SQLSec
    SQLSec --> ContainerSec
    
    ContainerSec --> NetSeg
    NetSeg --> Monitoring
    Monitoring --> Encryption
    
    Encryption --> DBEncryption
    DBEncryption --> BackupSec
    BackupSec --> DataGov
    
    DataGov --> SIEM
    SIEM --> SOC
    SOC --> Compliance
    
    style WebProxy fill:#ffcdd2
    style Auth fill:#e1f5fe
    style Vault fill:#f3e5f5
    style SIEM fill:#fff3e0
    style Compliance fill:#e8f5e8
```

## Data Flow Architecture

```mermaid
flowchart TD
    subgraph "Data Sources"
        ServerMetrics[Server Metrics<br/>CPU, Memory, Disk]
        AppLogs[Application Logs<br/>Management Server]
        SecurityEvents[Security Events<br/>OSQuery/FleetDM]
        SystemLogs[System Logs<br/>MaaS, Containers]
        AuditTrail[Audit Trail<br/>User Actions]
    end
    
    subgraph "Data Collection"
        Agents[Gough Agents<br/>Data Collection]
        Beats[Elastic Beats<br/>Log Shipping]
        Exporters[Prometheus Exporters<br/>Metrics Collection]
        OSQuery[OSQuery Agents<br/>Security Data]
    end
    
    subgraph "Data Processing"
        Logstash[Logstash<br/>Log Processing & Enrichment]
        Prometheus[Prometheus<br/>Metrics Aggregation]
        FleetDM[FleetDM<br/>Security Event Processing]
        ETL[ETL Jobs<br/>Data Transformation]
    end
    
    subgraph "Data Storage"
        TimeSeries[(Prometheus TSDB<br/>Time Series Data)]
        SearchEngine[(Elasticsearch<br/>Log & Event Data)]
        RDBMS[(PostgreSQL<br/>Operational Data)]
        SecurityDB[(MySQL<br/>Security Data)]
        Cache[(Redis<br/>Session & Cache Data)]
    end
    
    subgraph "Data Presentation"
        Grafana[Grafana<br/>Metrics Dashboards]
        Kibana[Kibana<br/>Log Analysis]
        WebUI[Management Portal<br/>System Status]
        SecurityDash[FleetDM UI<br/>Security Dashboard]
        Reports[Automated Reports<br/>PDF/Email]
    end
    
    subgraph "Data Consumers"
        Admins[System Administrators]
        DevOps[DevOps Engineers]
        Security[Security Analysts]
        Management[Management Team]
        External[External Systems<br/>SIEM, Ticketing]
    end
    
    ServerMetrics --> Agents
    AppLogs --> Beats
    SecurityEvents --> OSQuery
    SystemLogs --> Beats
    AuditTrail --> Beats
    
    Agents --> Prometheus
    Beats --> Logstash
    Exporters --> Prometheus
    OSQuery --> FleetDM
    
    Logstash --> SearchEngine
    Prometheus --> TimeSeries
    FleetDM --> SecurityDB
    ETL --> RDBMS
    ETL --> Cache
    
    TimeSeries --> Grafana
    SearchEngine --> Kibana
    RDBMS --> WebUI
    SecurityDB --> SecurityDash
    Cache --> WebUI
    
    Grafana --> Admins
    Grafana --> DevOps
    Kibana --> DevOps
    Kibana --> Security
    WebUI --> Admins
    WebUI --> Management
    SecurityDash --> Security
    Reports --> Management
    Reports --> External
    
    style ServerMetrics fill:#e3f2fd
    style Prometheus fill:#fff3e0
    style Grafana fill:#f3e5f5
    style RDBMS fill:#e8f5e8
    style Admins fill:#ffebee
```

## High Availability Architecture

```mermaid
graph TB
    subgraph "Load Balancer Tier"
        Internet[Internet] --> LB[Load Balancer<br/>HAProxy/Nginx<br/>Active-Active]
        LB --> VIP[Virtual IP<br/>Floating Address]
    end
    
    subgraph "Application Tier - Active/Active"
        VIP --> App1[Gough App 1<br/>Primary Node<br/>10.0.1.11]
        VIP --> App2[Gough App 2<br/>Secondary Node<br/>10.0.1.12]
        App1 -.->|Health Check| HealthCheck1[Health Monitor]
        App2 -.->|Health Check| HealthCheck2[Health Monitor]
    end
    
    subgraph "Database Tier - Master/Replica"
        App1 --> DBMaster[(PostgreSQL Master<br/>Read/Write<br/>10.0.1.21)]
        App2 --> DBMaster
        DBMaster --> DBReplica1[(PostgreSQL Replica 1<br/>Read Only<br/>10.0.1.22)]
        DBMaster --> DBReplica2[(PostgreSQL Replica 2<br/>Read Only<br/>10.0.1.23)]
        
        DBMaster -.->|Streaming Replication| DBReplica1
        DBMaster -.->|Streaming Replication| DBReplica2
    end
    
    subgraph "Cache Tier - Redis Cluster"
        App1 --> RedisCluster[Redis Cluster<br/>3 Masters, 3 Replicas]
        App2 --> RedisCluster
        
        subgraph "Redis Nodes"
            Redis1[Redis Master 1<br/>10.0.1.31]
            Redis2[Redis Master 2<br/>10.0.1.32]
            Redis3[Redis Master 3<br/>10.0.1.33]
            Redis4[Redis Replica 1<br/>10.0.1.34]
            Redis5[Redis Replica 2<br/>10.0.1.35]
            Redis6[Redis Replica 3<br/>10.0.1.36]
        end
        
        Redis1 -.-> Redis4
        Redis2 -.-> Redis5
        Redis3 -.-> Redis6
    end
    
    subgraph "Storage Tier - Shared Storage"
        DBMaster --> SharedStorage[Shared Storage<br/>NFS/GlusterFS/Ceph<br/>Redundant Storage]
        DBReplica1 --> SharedStorage
        DBReplica2 --> SharedStorage
        
        subgraph "Storage Cluster"
            Storage1[Storage Node 1]
            Storage2[Storage Node 2]
            Storage3[Storage Node 3]
        end
    end
    
    subgraph "Backup & DR"
        SharedStorage --> Backup[Automated Backups<br/>Daily/Weekly/Monthly]
        Backup --> OffSite[Off-Site Storage<br/>Cloud/Remote DC]
        
        subgraph "Disaster Recovery"
            DRSite[DR Site<br/>Standby Environment]
            DRReplication[Data Replication<br/>Async/Sync Options]
        end
        
        OffSite --> DRSite
        DBMaster -.->|DR Replication| DRReplication
        DRReplication --> DRSite
    end
    
    subgraph "Monitoring & Alerting"
        HealthCheck1 --> Monitoring[Monitoring System<br/>Prometheus/Grafana]
        HealthCheck2 --> Monitoring
        Monitoring --> Alerts[Alert Manager<br/>PagerDuty/Slack]
        
        subgraph "Failover Automation"
            Orchestrator[Failover Orchestrator<br/>Automatic Recovery]
            Monitor[Service Monitoring<br/>Health Checks]
        end
        
        Monitoring --> Monitor
        Monitor --> Orchestrator
        Orchestrator --> LB
        Orchestrator --> DBMaster
    end
    
    style LB fill:#ffcdd2
    style VIP fill:#f8bbd9
    style App1 fill:#e1f5fe
    style App2 fill:#e1f5fe
    style DBMaster fill:#c8e6c9
    style RedisCluster fill:#fff3e0
    style SharedStorage fill:#f3e5f5
    style Monitoring fill:#e8eaf6
```

This comprehensive set of architectural diagrams provides detailed visual representations of the Gough system's structure, data flows, security model, and high availability configuration. Each diagram serves a specific purpose:

1. **System Overview**: Complete system topology and component relationships
2. **Deployment Flow**: Step-by-step server provisioning process
3. **Network Architecture**: Network segmentation and communication paths
4. **Container Interaction**: Docker container communication and dependencies
5. **Security Architecture**: Multi-layered security controls and boundaries
6. **Data Flow**: Information flow from collection to presentation
7. **High Availability**: Redundancy and failover mechanisms

These diagrams can be used for:
- System documentation and training
- Architecture reviews and planning
- Troubleshooting and incident response
- Compliance and security audits
- Capacity planning and scaling decisions
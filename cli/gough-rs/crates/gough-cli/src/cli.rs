//! The complete clap command tree (23 groups, 91 leaf commands) and the global flags.
//!
//! This module only declares shape. Behaviour lives in `commands`; every leaf without a
//! handler fails loudly with the documented not-implemented exit code.

use std::path::PathBuf;

use clap::{Args, CommandFactory, Parser, Subcommand, ValueEnum};
use gough_core::{CoreError, ExitCode, OutputFormat};

const SECURITY_NOTE: &str = "\
Credentials never travel on argv: set GOUGH_TOKEN in the environment (or pipe the token on
stdin where a command supports it). TLS verification is always on; use --ca-file to trust an
extra CA. The tenant always comes from your token, never from a flag.";

/// Builds the root command, adding the security note and exit-code reference to `--help`.
pub fn command() -> clap::Command {
    Cli::command().after_help(format!("{SECURITY_NOTE}\n\n{}", ExitCode::help_table()))
}

fn parse_format(value: &str) -> Result<OutputFormat, String> {
    value.parse::<OutputFormat>().map_err(|e| e.to_string())
}

/// Root of the CLI: global flags plus one subcommand.
#[derive(Debug, Parser)]
#[command(
    name = "gough",
    about = "Hardware-layer orchestration for LXD clusters",
    disable_version_flag = true,
    arg_required_else_help = true,
    term_width = 100
)]
pub struct Cli {
    /// Print version information and exit.
    #[arg(long)]
    pub version: bool,
    /// Global flags shared by every command.
    #[command(flatten)]
    pub global: Global,
    /// Command to run.
    #[command(subcommand)]
    pub command: Option<Commands>,
}

/// Flags accepted by every command.
#[derive(Debug, Args)]
pub struct Global {
    /// Output format: table, json, yaml or jsonpath=<RFC 9535 expression>.
    #[arg(short = 'o', long = "output", global = true, default_value = "table",
          value_parser = parse_format, value_name = "FORMAT")]
    pub output: OutputFormat,
    /// Suppress all output except errors.
    #[arg(short = 'q', long, global = true)]
    pub quiet: bool,
    /// Disable colored output.
    #[arg(long, global = true)]
    pub no_color: bool,
    /// Cluster API URL (env GOUGH_CLUSTER_URL); overrides the config file.
    #[arg(long, global = true, value_name = "URL")]
    pub cluster: Option<String>,
    /// Named context from the config file (env GOUGH_CURRENT_CONTEXT).
    #[arg(long, global = true, value_name = "NAME")]
    pub context: Option<String>,
    /// Log requests and diagnostics to stderr (tokens are masked).
    #[arg(short = 'v', long, global = true)]
    pub verbose: bool,
    /// Extra PEM CA bundle to trust for the cluster's TLS certificate.
    #[arg(long, global = true, value_name = "FILE")]
    pub ca_file: Option<PathBuf>,
}

/// Top-level command groups and commands.
#[derive(Debug, Subcommand)]
pub enum Commands {
    /// Print version information.
    Version,
    /// Authenticate against the cluster (device flow).
    Login,
    /// Remove stored credentials.
    Logout,
    /// Read and write the local config file and contexts.
    #[command(subcommand)]
    Config(ConfigCmd),
    /// Bootstrap the primary node of a new cluster.
    Init {
        /// Deploy a highly-available control plane.
        #[arg(long)]
        ha: bool,
        /// Make this cluster the authoritative DHCP server.
        #[arg(long)]
        dhcp_authoritative: bool,
        /// Restore the cluster from an S3 backup URL.
        #[arg(long, value_name = "S3URL")]
        restore_from: Option<String>,
    },
    /// Export or import cluster state.
    #[command(subcommand)]
    Sync(SyncCmd),
    /// Vault operations.
    #[command(subcommand)]
    Vault(VaultCmd),
    /// Primary-node lifecycle operations.
    #[command(subcommand)]
    Primary(PrimaryCmd),
    /// Manage nodes.
    #[command(subcommand)]
    Node(NodeCmd),
    /// Manage node disks.
    #[command(subcommand)]
    Disk(DiskCmd),
    /// Manage biomes (workload definitions).
    #[command(subcommand)]
    Biome(BiomeCmd),
    /// Cluster-wide operations.
    #[command(subcommand)]
    Cluster(ClusterCmd),
    /// LXD trust-password operations.
    #[command(subcommand)]
    Lxd(LxdCmd),
    /// Capacity forecasting and risk.
    #[command(subcommand)]
    Capacity(CapacityCmd),
    /// Workload migration policy and triggers.
    #[command(subcommand)]
    Migration(MigrationCmd),
    /// Disaster recovery.
    #[command(subcommand)]
    Dr(DrCmd),
    /// Restore a cluster from backup (same as `dr restore`).
    Restore(RestoreArgs),
    /// Audit log operations.
    #[command(subcommand)]
    Audit(AuditCmd),
    /// Development helpers (simulated nodes, seed data).
    #[command(subcommand)]
    Dev(DevCmd),
    /// Third-party integrations.
    #[command(subcommand)]
    Integrations(IntegrationsCmd),
    /// Run diagnostic checks.
    #[command(subcommand)]
    Doctor(DoctorCmd),
    /// Webhook operations.
    #[command(subcommand)]
    Webhook(WebhookCmd),
    /// Manage deployments.
    #[command(subcommand)]
    Deployment(DeploymentCmd),
    /// Storage quotas.
    #[command(subcommand)]
    Storage(StorageCmd),
    /// Generate shell completion scripts.
    #[command(subcommand)]
    Completion(CompletionCmd),
}

/// `gough config` subcommands.
#[derive(Debug, Subcommand)]
pub enum ConfigCmd {
    /// Print the effective value of a config key.
    Get {
        /// Key: cluster.url, tenant_id, current_context, contexts.<name>.cluster_url, contexts.<name>.tenant_id.
        key: String,
    },
    /// Set a config key in the config file.
    Set {
        /// Config key (see `config get`).
        key: String,
        /// New value.
        value: String,
    },
    /// List effective config values and where each came from.
    List,
    /// List named contexts.
    GetContexts,
    /// Print the active context name.
    CurrentContext,
    /// Switch the active context.
    UseContext {
        /// Context name.
        name: String,
    },
    /// Create or update a context.
    SetContext {
        /// Context name.
        name: String,
        /// Cluster API URL for this context.
        #[arg(short = 'c', long, required = true, value_name = "URL")]
        cluster_url: String,
        /// Tenant hint for this context (display only).
        #[arg(short = 't', long)]
        tenant: Option<String>,
    },
    /// Delete a context.
    DeleteContext {
        /// Context name.
        name: String,
    },
}

/// `gough sync` subcommands.
#[derive(Debug, Subcommand)]
pub enum SyncCmd {
    /// Export cluster state to a directory.
    Export {
        /// Destination directory.
        dir: PathBuf,
    },
    /// Import cluster state from a directory.
    Import {
        /// Source directory.
        dir: PathBuf,
    },
}

/// `gough vault` subcommands.
#[derive(Debug, Subcommand)]
pub enum VaultCmd {
    /// Submit an unseal share (read from a hidden prompt or stdin).
    Unseal,
}

/// Frontend switch mode for `primary frontend-switch`.
#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum SwitchMode {
    /// Virtual IP.
    Vip,
    /// Anycast.
    Anycast,
}

/// `gough primary` subcommands.
#[derive(Debug, Subcommand)]
pub enum PrimaryCmd {
    /// Replace the primary node.
    Replace {
        /// Node to promote.
        node_id: String,
    },
    /// Force recovery from a surviving node.
    ForceRecover {
        /// Surviving node id.
        #[arg(long, required = true)]
        surviving_node: String,
        /// Why recovery is forced (audit logged).
        #[arg(long, required = true)]
        reason: String,
    },
    /// Switch the frontend between VIP and anycast.
    FrontendSwitch {
        /// Target mode.
        #[arg(long, required = true, value_enum)]
        mode: SwitchMode,
    },
    /// Rotate the cluster CA.
    RotateCa,
}

/// `gough node` subcommands.
#[derive(Debug, Subcommand)]
pub enum NodeCmd {
    /// List nodes.
    List {
        /// Filter by node state.
        #[arg(long)]
        state: Option<String>,
        /// Filter by tenant (cross-tenant administrators only).
        #[arg(long)]
        tenant: Option<String>,
        /// Filter by tag.
        #[arg(long, value_name = "KEY=VALUE")]
        tag: Option<String>,
    },
    /// Show one node.
    Show {
        /// Node id.
        id: String,
    },
    /// Deploy a plan to a node.
    Deploy {
        /// Node id.
        id: String,
        /// Plan file (JSON).
        #[arg(long, value_name = "FILE")]
        plan: Option<PathBuf>,
    },
    /// Reject a node enrollment.
    Reject {
        /// Node id.
        id: String,
        /// Reason (audit logged).
        #[arg(long, required = true)]
        reason: String,
    },
    /// Decommission a node.
    Decommission {
        /// Node id.
        id: String,
        /// Reason (audit logged).
        #[arg(long, required = true)]
        reason: String,
    },
    /// Evacuate workloads from a node.
    Evacuate {
        /// Node id.
        id: String,
    },
    /// Rotate a node's keys.
    Rekey {
        /// Node id.
        id: String,
        /// Reason: rotation, compromise or compliance.
        #[arg(long, required = true)]
        reason: String,
    },
    /// Add or remove node tags.
    #[command(subcommand)]
    Tag(NodeTagCmd),
    /// List a node's tags.
    Tags {
        /// Node id.
        id: String,
    },
}

/// `gough node tag` subcommands.
#[derive(Debug, Subcommand)]
pub enum NodeTagCmd {
    /// Add a tag.
    Add {
        /// Node id.
        id: String,
        /// Tag as key=value.
        tag: String,
    },
    /// Remove a tag.
    Remove {
        /// Node id.
        id: String,
        /// Tag as key=value.
        tag: String,
    },
}

/// `gough disk` subcommands.
#[derive(Debug, Subcommand)]
pub enum DiskCmd {
    /// List a node's disks.
    List {
        /// Node id.
        node_id: String,
    },
    /// Apply a disk plan to a node.
    Plan {
        /// Node id.
        node_id: String,
        /// Plan file.
        #[arg(long, required = true, value_name = "FILE")]
        plan: PathBuf,
    },
    /// Re-run a SMART check on a disk.
    SmartRecheck {
        /// Node id.
        node_id: String,
        /// Disk id.
        disk_id: String,
    },
}

/// Frontend mode for `biome deploy`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum FrontendMode {
    /// kube-vip managed virtual IP.
    KubeVip,
    /// An external load balancer.
    External,
    /// No frontend.
    None,
}

/// `gough biome` subcommands.
#[derive(Debug, Subcommand)]
pub enum BiomeCmd {
    /// List biomes.
    List {
        /// Filter by biome kind.
        #[arg(long)]
        kind: Option<String>,
        /// Filter by phase.
        #[arg(long)]
        phase: Option<String>,
        /// Filter by workload type.
        #[arg(long)]
        workload: Option<String>,
    },
    /// Show a biome.
    Show {
        /// Biome name.
        name: String,
        /// Biome version (default: latest).
        #[arg(long = "version", value_name = "VERSION")]
        biome_version: Option<String>,
    },
    /// Scaffold a new biome locally.
    New {
        /// Biome name.
        name: String,
    },
    /// Validate the biome in the current directory.
    Validate {
        /// Also run the biome's tests.
        #[arg(long)]
        test: bool,
    },
    /// Publish the biome in the current directory.
    Publish,
    /// Promote a biome version.
    Promote {
        /// Biome name.
        name: String,
        /// Version to promote.
        version: String,
    },
    /// Roll a biome back to its previous version.
    Rollback {
        /// Biome name.
        name: String,
    },
    /// Diff two biome versions.
    Diff {
        /// Biome name.
        name: String,
        /// First version.
        v1: String,
        /// Second version.
        v2: String,
    },
    /// Re-sign a biome version.
    ReSign {
        /// Biome name.
        name: String,
        /// Version to re-sign.
        version: String,
    },
    /// Check whether a biome instance may be upgraded or moved.
    EligibilityCheck {
        /// Biome instance id.
        instance_id: String,
    },
    /// Upgrade a biome instance.
    Upgrade {
        /// Biome name.
        name: String,
        /// Target version.
        #[arg(long, required = true)]
        to: String,
    },
    /// Deploy a biome to a node.
    Deploy {
        /// Biome name.
        name: String,
        /// Target node id.
        #[arg(long, required = true)]
        node: String,
        /// Frontend mode.
        #[arg(long, value_enum, default_value = "kube-vip")]
        frontend_mode: FrontendMode,
        /// Frontend endpoint (required for kube-vip and external; forbidden for none).
        #[arg(long)]
        frontend_endpoint: Option<String>,
        /// Frontend network interface.
        #[arg(long)]
        frontend_interface: Option<String>,
        /// Frontend network baseline (default: external when kube-vip).
        #[arg(long)]
        frontend_baseline: Option<String>,
    },
}

/// Identity-plane provider.
#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum IdentityProvider {
    /// Built-in provider.
    Builtin,
    /// Skauswatch provider.
    Skauswatch,
    /// External provider.
    External,
}

/// Kind of existing infrastructure to adopt.
#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum AdoptKind {
    /// Kubernetes.
    K8s,
    /// LXD.
    Lxd,
    /// Ceph.
    Ceph,
    /// Longhorn.
    Longhorn,
}

/// `gough cluster` subcommands.
#[derive(Debug, Subcommand)]
pub enum ClusterCmd {
    /// Show cluster health.
    Status,
    /// Upgrade the cluster.
    Upgrade,
    /// Evacuate a node's workloads.
    Evacuate {
        /// Node id.
        node_id: String,
    },
    /// Adopt existing infrastructure.
    Adopt {
        /// What to adopt.
        #[arg(value_enum)]
        kind: AdoptKind,
        /// Option as key=value (repeatable).
        #[arg(long = "opt", value_name = "KEY=VALUE")]
        opts: Vec<String>,
    },
    /// Rotate joiner secrets.
    RotateJoinerSecrets {
        /// Only rotate secrets for this biome kind.
        #[arg(long)]
        biome_kind: Option<String>,
    },
    /// Identity-plane operations.
    #[command(subcommand)]
    IdentityPlane(IdentityPlaneCmd),
    /// Network-baseline operations.
    #[command(subcommand)]
    NetworkBaseline(NetworkBaselineCmd),
    /// Show the allowed tag vocabulary.
    TagVocabulary,
}

/// `gough cluster identity-plane` subcommands.
#[derive(Debug, Subcommand)]
pub enum IdentityPlaneCmd {
    /// Show identity-plane status.
    Status,
    /// Configure the identity-plane provider.
    Configure {
        /// Provider.
        #[arg(long, required = true, value_enum)]
        provider: IdentityProvider,
    },
}

/// `gough cluster network-baseline` subcommands.
#[derive(Debug, Subcommand)]
pub enum NetworkBaselineCmd {
    /// Show network-baseline status.
    Status,
    /// Configure a network baseline.
    Configure {
        /// Baseline name.
        baseline: String,
        /// Option as key=value (repeatable).
        #[arg(long = "opt", value_name = "KEY=VALUE")]
        opts: Vec<String>,
    },
    /// Migrate to a different baseline.
    Migrate {
        /// Current baseline name.
        baseline: String,
        /// Target baseline.
        #[arg(long, required = true)]
        to: String,
    },
}

/// `gough lxd` subcommands.
#[derive(Debug, Subcommand)]
pub enum LxdCmd {
    /// Reveal the LXD trust password (audit logged).
    ShowTrustPassword {
        /// Why access is needed.
        #[arg(long, required = true)]
        reason: String,
    },
    /// Rotate the LXD trust password.
    RotateTrustPassword,
}

/// `gough capacity` subcommands.
#[derive(Debug, Subcommand)]
pub enum CapacityCmd {
    /// Forecast node utilization.
    Forecast {
        /// Forecast horizon in days (documented values: 1, 7, 30).
        #[arg(long, default_value_t = 7, value_parser = clap::value_parser!(u32).range(1..))]
        horizon_days: u32,
    },
    /// List nodes at risk of exhausting capacity.
    Risks,
}

/// `gough migration` subcommands.
#[derive(Debug, Subcommand)]
pub enum MigrationCmd {
    /// Show or change the migration policy.
    #[command(subcommand)]
    Policy(MigrationPolicyCmd),
    /// Trigger a migration of a biome instance.
    Trigger {
        /// Biome instance id.
        instance_id: String,
        /// Target node id.
        #[arg(long)]
        target: Option<String>,
        /// Ignore migration locks.
        #[arg(long)]
        ignore_lock: bool,
        /// Why the migration is triggered.
        #[arg(long)]
        reason: Option<String>,
    },
}

/// `gough migration policy` subcommands.
#[derive(Debug, Subcommand)]
pub enum MigrationPolicyCmd {
    /// Show the migration policy.
    Show,
    /// Set one policy field.
    Set {
        /// Policy field name.
        field: String,
        /// New value.
        value: String,
    },
}

/// Arguments shared by `dr restore` and `restore`.
#[derive(Debug, Args)]
pub struct RestoreArgs {
    /// Backup source (S3 URL).
    #[arg(long, required = true)]
    pub from: String,
    /// Cluster id to restore.
    #[arg(long, required = true)]
    pub cluster_id: String,
}

/// `gough dr` subcommands.
#[derive(Debug, Subcommand)]
pub enum DrCmd {
    /// Run a disaster-recovery drill.
    Drill {
        /// Drill target site.
        #[arg(long)]
        target: Option<String>,
    },
    /// Promote a DR site.
    Promote {
        /// Site id.
        site_id: String,
        /// Why the site is promoted (audit logged).
        #[arg(long, required = true)]
        reason: String,
        /// The source site is unreachable.
        #[arg(long)]
        source_unreachable: bool,
    },
    /// Fail back to the original site.
    Failback {
        /// Site id.
        site_id: String,
    },
    /// Restore a cluster from backup.
    Restore(RestoreArgs),
}

/// `gough audit` subcommands.
#[derive(Debug, Subcommand)]
pub enum AuditCmd {
    /// List audit events.
    List {
        /// Only events since this ISO-8601 timestamp.
        #[arg(long)]
        since: Option<String>,
        /// Filter by tenant (cross-tenant administrators only).
        #[arg(long)]
        tenant: Option<String>,
    },
    /// Verify the audit chain.
    Verify {
        /// Verify from this ISO-8601 timestamp.
        #[arg(long)]
        since: Option<String>,
        /// Verify up to this ISO-8601 timestamp.
        #[arg(long)]
        to: Option<String>,
        /// Verify a previously exported file offline.
        #[arg(long, value_name = "FILE")]
        input: Option<PathBuf>,
    },
    /// Export the audit log.
    Export {
        /// Only events since this ISO-8601 timestamp.
        #[arg(long)]
        since: Option<String>,
        /// Destination file.
        #[arg(short = 'f', long = "file", required = true, value_name = "FILE")]
        file: PathBuf,
    },
}

/// `gough dev` subcommands.
#[derive(Debug, Subcommand)]
pub enum DevCmd {
    /// Create a simulated node.
    NodeSim {
        /// CPU architecture.
        #[arg(long, default_value = "amd64")]
        arch: String,
        /// Firmware type.
        #[arg(long, default_value = "uefi")]
        firmware: String,
        /// Enable IPv6.
        #[arg(long)]
        ipv6: bool,
    },
    /// Seed biomes into a development cluster.
    Seed {
        /// Comma-separated biome names.
        #[arg(long, required = true, value_delimiter = ',')]
        biomes: Vec<String>,
    },
    /// Reset a development cluster.
    Reset,
}

/// `gough integrations` subcommands.
#[derive(Debug, Subcommand)]
pub enum IntegrationsCmd {
    /// Show integration status.
    Status,
    /// Configure an integration.
    Configure {
        /// Integration name.
        name: String,
        /// Option as key=value (repeatable).
        #[arg(long = "opt", value_name = "KEY=VALUE")]
        opts: Vec<String>,
    },
    /// Rotate an integration's credentials.
    RotateCredentials {
        /// Integration name.
        name: String,
    },
    /// Validate an integration's granted scope.
    ValidateScope {
        /// Integration name.
        name: String,
    },
}

/// `gough doctor` subcommands.
#[derive(Debug, Subcommand)]
pub enum DoctorCmd {
    /// Check network configuration.
    Network,
    /// Check the helper image.
    HelperImage,
    /// Check encryption status.
    EncryptionStatus,
    /// Inventory cryptographic material.
    CryptoInventory,
}

/// `gough webhook` subcommands.
#[derive(Debug, Subcommand)]
pub enum WebhookCmd {
    /// Send a test event to a webhook.
    Test {
        /// Webhook id.
        webhook_id: String,
    },
}

/// `gough deployment` subcommands.
#[derive(Debug, Subcommand)]
pub enum DeploymentCmd {
    /// List deployments.
    List {
        /// Filter by status.
        #[arg(long)]
        status: Option<String>,
        /// Filter by biome id.
        #[arg(long)]
        biome_id: Option<String>,
        /// Filter by node id.
        #[arg(long)]
        node_id: Option<String>,
    },
    /// Show a deployment's logs.
    Logs {
        /// Deployment id.
        id: String,
        /// Number of trailing lines.
        #[arg(long, default_value_t = 100)]
        tail: u32,
    },
    /// Cancel a deployment.
    Cancel {
        /// Deployment id.
        id: String,
    },
}

/// `gough storage` subcommands.
#[derive(Debug, Subcommand)]
pub enum StorageCmd {
    /// List storage quotas.
    ListQuotas {
        /// Filter by tenant id.
        #[arg(long)]
        tenant_id: Option<String>,
    },
    /// Request a quota increase.
    QuotaRequest {
        /// Tenant id.
        #[arg(long)]
        tenant_id: Option<String>,
        /// Resource type.
        #[arg(long)]
        resource_type: Option<String>,
        /// Requested value.
        #[arg(long)]
        requested_value: Option<f64>,
        /// Unit of the requested value.
        #[arg(long)]
        unit: Option<String>,
        /// Why the increase is needed.
        #[arg(long)]
        justification: Option<String>,
    },
}

/// Shells supported by `gough completion`.
#[derive(Debug, Subcommand)]
pub enum CompletionCmd {
    /// Generate the bash completion script.
    Bash,
    /// Generate the zsh completion script.
    Zsh,
    /// Generate the fish completion script.
    Fish,
    /// Generate the PowerShell completion script.
    Powershell,
}

impl FrontendMode {
    /// Client-side rule: kube-vip and external need an endpoint, `none` forbids one.
    pub fn validate_endpoint(self, endpoint: Option<&str>) -> Result<(), CoreError> {
        match (self, endpoint) {
            (FrontendMode::None, Some(_)) => Err(CoreError::Usage(
                "--frontend-endpoint is not allowed with --frontend-mode none".into(),
            )),
            (FrontendMode::KubeVip | FrontendMode::External, None) => Err(CoreError::Usage(
                "--frontend-endpoint is required with --frontend-mode kube-vip or external".into(),
            )),
            _ => Ok(()),
        }
    }
}

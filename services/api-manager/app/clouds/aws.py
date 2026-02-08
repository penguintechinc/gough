"""AWS EC2 Cloud Provider Implementation.

Provides machine lifecycle management for AWS EC2 instances with support
for IAM role or access key authentication, VPC/Security groups, and cloud-init.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from .base import (
    BaseCloud,
    CloudAuthError,
    CloudError,
    CloudNotFoundError,
    CloudQuotaError,
    Machine,
    MachineSpec,
    MachineState,
)

log = logging.getLogger(__name__)


# Map EC2 instance states to unified MachineState
EC2_STATE_MAP: dict[str, MachineState] = {
    "pending": MachineState.PENDING,
    "running": MachineState.RUNNING,
    "stopping": MachineState.PENDING,
    "stopped": MachineState.STOPPED,
    "shutting-down": MachineState.PENDING,
    "terminated": MachineState.TERMINATED,
}


class AWSCloud(BaseCloud):
    """AWS EC2 cloud provider implementation.

    Supports both IAM role-based authentication (recommended for EC2 instances)
    and access key authentication for development/external environments.

    Config Parameters:
        AWS_REGION: AWS region (e.g., us-east-1) - required
        AWS_ACCESS_KEY_ID: Access key ID (optional if using IAM role)
        AWS_SECRET_ACCESS_KEY: Secret access key (optional if using IAM role)
        AWS_SESSION_TOKEN: Session token for temporary credentials (optional)
        AWS_DEFAULT_VPC: Default VPC ID for instances (optional)
        AWS_DEFAULT_SECURITY_GROUPS: Comma-separated security group IDs (optional)
        AWS_DEFAULT_SUBNET: Default subnet ID (optional)
        AWS_DEFAULT_KEY_NAME: Default SSH key pair name (optional)
    """

    provider_type: str = "aws"
    supports_cloud_init: bool = True

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize AWS cloud provider.

        Args:
            config: AWS configuration dictionary

        Raises:
            CloudError: If required configuration is missing
        """
        super().__init__(config)

        self.region = config.get("AWS_REGION", "")
        if not self.region:
            raise CloudError("AWS_REGION is required")

        self.access_key_id = config.get("AWS_ACCESS_KEY_ID")
        self.secret_access_key = config.get("AWS_SECRET_ACCESS_KEY")
        self.session_token = config.get("AWS_SESSION_TOKEN")

        # Default resources
        self.default_vpc = config.get("AWS_DEFAULT_VPC")
        self.default_security_groups = config.get("AWS_DEFAULT_SECURITY_GROUPS", "")
        self.default_subnet = config.get("AWS_DEFAULT_SUBNET")
        self.default_key_name = config.get("AWS_DEFAULT_KEY_NAME")

        # Provider ID for machine objects
        self.provider_id = config.get("provider_id", "")

        # Boto3 clients (initialized on authenticate)
        self._ec2_client: Any = None
        self._ec2_resource: Any = None

    def _get_session(self) -> boto3.Session:
        """Create a boto3 session with configured credentials.

        Returns:
            boto3.Session: Configured AWS session
        """
        session_kwargs: dict[str, Any] = {"region_name": self.region}

        # Use explicit credentials if provided, otherwise rely on IAM role
        if self.access_key_id and self.secret_access_key:
            session_kwargs["aws_access_key_id"] = self.access_key_id
            session_kwargs["aws_secret_access_key"] = self.secret_access_key
            if self.session_token:
                session_kwargs["aws_session_token"] = self.session_token

        return boto3.Session(**session_kwargs)

    def authenticate(self) -> bool:
        """Authenticate with AWS and verify credentials.

        Returns:
            True if authentication successful

        Raises:
            CloudAuthError: If authentication fails
        """
        try:
            session = self._get_session()
            self._ec2_client = session.client("ec2")
            self._ec2_resource = session.resource("ec2")

            # Verify credentials by making a simple API call
            self._ec2_client.describe_regions(RegionNames=[self.region])

            self._authenticated = True
            log.info(f"Successfully authenticated with AWS in region {self.region}")
            return True

        except NoCredentialsError as e:
            raise CloudAuthError(
                "No AWS credentials found. Provide AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY, or ensure IAM role is attached."
            ) from e
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code in ("AuthFailure", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                raise CloudAuthError(f"AWS authentication failed: {error_msg}") from e
            raise CloudError(f"AWS API error during authentication: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def _ensure_authenticated(self) -> None:
        """Ensure we are authenticated before making API calls."""
        if not self._authenticated or self._ec2_client is None:
            self.authenticate()

    def _parse_instance(self, instance: dict[str, Any]) -> Machine:
        """Parse EC2 instance data into a Machine object.

        Args:
            instance: EC2 instance dictionary from describe_instances

        Returns:
            Machine object
        """
        instance_id = instance.get("InstanceId", "")
        state_name = instance.get("State", {}).get("Name", "unknown")
        machine_state = EC2_STATE_MAP.get(state_name, MachineState.UNKNOWN)

        # Extract name from tags
        name = ""
        tags_dict: dict[str, str] = {}
        for tag in instance.get("Tags", []):
            key = tag.get("Key", "")
            value = tag.get("Value", "")
            tags_dict[key] = value
            if key == "Name":
                name = value

        # Parse IPs
        public_ips: list[str] = []
        private_ips: list[str] = []

        if instance.get("PublicIpAddress"):
            public_ips.append(instance["PublicIpAddress"])
        if instance.get("PrivateIpAddress"):
            private_ips.append(instance["PrivateIpAddress"])

        # Add IPs from network interfaces
        for eni in instance.get("NetworkInterfaces", []):
            for private_ip_info in eni.get("PrivateIpAddresses", []):
                private_addr = private_ip_info.get("PrivateIpAddress")
                if private_addr and private_addr not in private_ips:
                    private_ips.append(private_addr)
                association = private_ip_info.get("Association", {})
                public_addr = association.get("PublicIp")
                if public_addr and public_addr not in public_ips:
                    public_ips.append(public_addr)

        # Parse timestamps
        launch_time = instance.get("LaunchTime")
        created_at = None
        if launch_time:
            if isinstance(launch_time, datetime):
                created_at = launch_time
            elif isinstance(launch_time, str):
                created_at = datetime.fromisoformat(launch_time.replace("Z", "+00:00"))

        # Build extra data
        extra: dict[str, Any] = {
            "instance_type": instance.get("InstanceType", ""),
            "ami_id": instance.get("ImageId", ""),
            "vpc_id": instance.get("VpcId", ""),
            "subnet_id": instance.get("SubnetId", ""),
            "availability_zone": instance.get("Placement", {}).get("AvailabilityZone", ""),
            "key_name": instance.get("KeyName", ""),
            "security_groups": [
                {"id": sg.get("GroupId", ""), "name": sg.get("GroupName", "")}
                for sg in instance.get("SecurityGroups", [])
            ],
            "architecture": instance.get("Architecture", ""),
            "platform": instance.get("PlatformDetails", ""),
            "root_device_type": instance.get("RootDeviceType", ""),
            "root_device_name": instance.get("RootDeviceName", ""),
            "state_reason": instance.get("StateReason", {}).get("Message", ""),
        }

        return Machine(
            id=instance_id,
            name=name or instance_id,
            state=machine_state,
            provider="aws",
            provider_id=self.provider_id,
            region=instance.get("Placement", {}).get("AvailabilityZone", self.region),
            image=instance.get("ImageId", ""),
            size=instance.get("InstanceType", ""),
            public_ips=public_ips,
            private_ips=private_ips,
            created_at=created_at,
            updated_at=datetime.now(timezone.utc),
            tags=tags_dict,
            extra=extra,
        )

    def list_machines(self, filters: dict | None = None) -> list[Machine]:
        """List all EC2 instances.

        Args:
            filters: Optional filters. Supports:
                - instance_ids: List of instance IDs
                - states: List of states to filter
                - tags: Dict of tag key-value pairs
                - vpc_id: Filter by VPC ID
                - subnet_id: Filter by subnet ID

        Returns:
            List of Machine objects

        Raises:
            CloudError: On API errors
        """
        self._ensure_authenticated()

        ec2_filters: list[dict[str, Any]] = []

        if filters:
            # Filter by instance IDs
            instance_ids = filters.get("instance_ids", [])

            # Filter by states
            states = filters.get("states", [])
            if states:
                ec2_filters.append({"Name": "instance-state-name", "Values": states})

            # Filter by tags
            tags = filters.get("tags", {})
            for key, value in tags.items():
                ec2_filters.append({"Name": f"tag:{key}", "Values": [value]})

            # Filter by VPC
            vpc_id = filters.get("vpc_id")
            if vpc_id:
                ec2_filters.append({"Name": "vpc-id", "Values": [vpc_id]})

            # Filter by subnet
            subnet_id = filters.get("subnet_id")
            if subnet_id:
                ec2_filters.append({"Name": "subnet-id", "Values": [subnet_id]})
        else:
            instance_ids = []

        try:
            paginator = self._ec2_client.get_paginator("describe_instances")

            describe_kwargs: dict[str, Any] = {}
            if instance_ids:
                describe_kwargs["InstanceIds"] = instance_ids
            if ec2_filters:
                describe_kwargs["Filters"] = ec2_filters

            machines: list[Machine] = []

            for page in paginator.paginate(**describe_kwargs):
                for reservation in page.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        machines.append(self._parse_instance(instance))

            log.debug(f"Listed {len(machines)} EC2 instances")
            return machines

        except ClientError as e:
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            raise CloudError(f"Failed to list EC2 instances: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def get_machine(self, machine_id: str) -> Machine:
        """Get a specific EC2 instance by ID.

        Args:
            machine_id: EC2 instance ID (i-xxxxxxxxxxxxxxxxx)

        Returns:
            Machine object

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            response = self._ec2_client.describe_instances(InstanceIds=[machine_id])

            reservations = response.get("Reservations", [])
            if not reservations:
                raise CloudNotFoundError(f"Instance not found: {machine_id}")

            instances = reservations[0].get("Instances", [])
            if not instances:
                raise CloudNotFoundError(f"Instance not found: {machine_id}")

            return self._parse_instance(instances[0])

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code == "InvalidInstanceID.NotFound":
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            if error_code == "InvalidInstanceID.Malformed":
                raise CloudNotFoundError(f"Invalid instance ID format: {machine_id}") from e
            raise CloudError(f"Failed to get instance {machine_id}: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def create_machine(self, spec: MachineSpec) -> Machine:
        """Create a new EC2 instance.

        Args:
            spec: Machine specification. Extra fields supported:
                - key_name: SSH key pair name
                - subnet_id: Subnet ID to launch in
                - security_group_ids: List of security group IDs
                - vpc_id: VPC ID (for creating security groups)
                - iam_instance_profile: IAM instance profile name/ARN
                - ebs_optimized: Enable EBS optimization
                - monitoring: Enable detailed monitoring
                - associate_public_ip: Associate public IP address

        Returns:
            Created Machine object

        Raises:
            CloudQuotaError: If quota exceeded
            CloudError: On API errors
        """
        self._ensure_authenticated()

        # Build run_instances parameters
        run_kwargs: dict[str, Any] = {
            "ImageId": spec.image,
            "InstanceType": spec.size,
            "MinCount": 1,
            "MaxCount": 1,
        }

        # Instance name via tags
        tags: list[dict[str, str]] = [{"Key": "Name", "Value": spec.name}]
        for key, value in spec.tags.items():
            tags.append({"Key": key, "Value": value})

        run_kwargs["TagSpecifications"] = [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ]

        # Cloud-init user data
        if spec.cloud_init:
            # User data must be base64 encoded
            run_kwargs["UserData"] = base64.b64encode(
                spec.cloud_init.encode("utf-8")
            ).decode("ascii")

        # SSH key pair
        key_name = spec.extra.get("key_name", self.default_key_name)
        if key_name:
            run_kwargs["KeyName"] = key_name

        # Network configuration
        subnet_id = spec.extra.get("subnet_id", self.default_subnet)
        if subnet_id:
            run_kwargs["SubnetId"] = subnet_id

        # Security groups
        security_groups: list[str] = spec.extra.get("security_group_ids", [])
        if not security_groups and self.default_security_groups:
            security_groups = [
                sg.strip()
                for sg in self.default_security_groups.split(",")
                if sg.strip()
            ]
        if security_groups:
            run_kwargs["SecurityGroupIds"] = security_groups

        # Optional: IAM instance profile
        iam_profile = spec.extra.get("iam_instance_profile")
        if iam_profile:
            if iam_profile.startswith("arn:"):
                run_kwargs["IamInstanceProfile"] = {"Arn": iam_profile}
            else:
                run_kwargs["IamInstanceProfile"] = {"Name": iam_profile}

        # Optional: EBS optimization
        if spec.extra.get("ebs_optimized"):
            run_kwargs["EbsOptimized"] = True

        # Optional: Detailed monitoring
        if spec.extra.get("monitoring"):
            run_kwargs["Monitoring"] = {"Enabled": True}

        # Optional: Public IP association
        associate_public_ip = spec.extra.get("associate_public_ip")
        if associate_public_ip is not None and subnet_id:
            # Network interface config needed for public IP in VPC
            run_kwargs["NetworkInterfaces"] = [
                {
                    "DeviceIndex": 0,
                    "SubnetId": subnet_id,
                    "AssociatePublicIpAddress": associate_public_ip,
                    "Groups": security_groups if security_groups else [],
                }
            ]
            # Remove these as they conflict with NetworkInterfaces
            run_kwargs.pop("SubnetId", None)
            run_kwargs.pop("SecurityGroupIds", None)

        # Root volume configuration
        if spec.storage_gb > 0:
            run_kwargs["BlockDeviceMappings"] = [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": spec.storage_gb,
                        "VolumeType": spec.extra.get("volume_type", "gp3"),
                        "DeleteOnTermination": True,
                    },
                }
            ]

        try:
            log.info(f"Creating EC2 instance '{spec.name}' with type {spec.size}")
            response = self._ec2_client.run_instances(**run_kwargs)

            instances = response.get("Instances", [])
            if not instances:
                raise CloudError("No instance created in response")

            instance = instances[0]
            instance_id = instance.get("InstanceId", "")

            log.info(f"Created EC2 instance: {instance_id}")

            # Fetch full instance details
            return self.get_machine(instance_id)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            # Check for quota/limit errors
            quota_errors = [
                "InstanceLimitExceeded",
                "InsufficientInstanceCapacity",
                "MaxSpotInstanceCountExceeded",
                "VcpuLimitExceeded",
            ]
            if error_code in quota_errors:
                raise CloudQuotaError(f"AWS quota exceeded: {error_msg}") from e

            raise CloudError(f"Failed to create EC2 instance: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def destroy_machine(self, machine_id: str) -> bool:
        """Terminate an EC2 instance.

        Args:
            machine_id: EC2 instance ID

        Returns:
            True if terminated successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            log.info(f"Terminating EC2 instance: {machine_id}")
            response = self._ec2_client.terminate_instances(InstanceIds=[machine_id])

            terminating = response.get("TerminatingInstances", [])
            if not terminating:
                raise CloudNotFoundError(f"Instance not found: {machine_id}")

            current_state = terminating[0].get("CurrentState", {}).get("Name", "")
            log.info(f"Instance {machine_id} termination initiated, state: {current_state}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            raise CloudError(f"Failed to terminate instance {machine_id}: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def start_machine(self, machine_id: str) -> bool:
        """Start a stopped EC2 instance.

        Args:
            machine_id: EC2 instance ID

        Returns:
            True if started successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            log.info(f"Starting EC2 instance: {machine_id}")
            response = self._ec2_client.start_instances(InstanceIds=[machine_id])

            starting = response.get("StartingInstances", [])
            if not starting:
                raise CloudNotFoundError(f"Instance not found: {machine_id}")

            current_state = starting[0].get("CurrentState", {}).get("Name", "")
            log.info(f"Instance {machine_id} start initiated, state: {current_state}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            if error_code == "IncorrectInstanceState":
                raise CloudError(
                    f"Instance {machine_id} cannot be started in current state"
                ) from e
            raise CloudError(f"Failed to start instance {machine_id}: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def stop_machine(self, machine_id: str) -> bool:
        """Stop a running EC2 instance.

        Args:
            machine_id: EC2 instance ID

        Returns:
            True if stopped successfully

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            log.info(f"Stopping EC2 instance: {machine_id}")
            response = self._ec2_client.stop_instances(InstanceIds=[machine_id])

            stopping = response.get("StoppingInstances", [])
            if not stopping:
                raise CloudNotFoundError(f"Instance not found: {machine_id}")

            current_state = stopping[0].get("CurrentState", {}).get("Name", "")
            log.info(f"Instance {machine_id} stop initiated, state: {current_state}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            if error_code == "IncorrectInstanceState":
                raise CloudError(
                    f"Instance {machine_id} cannot be stopped in current state"
                ) from e
            if error_code == "UnsupportedOperation":
                raise CloudError(
                    f"Instance {machine_id} is a spot instance and cannot be stopped"
                ) from e
            raise CloudError(f"Failed to stop instance {machine_id}: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def reboot_machine(self, machine_id: str) -> bool:
        """Reboot an EC2 instance.

        Uses native EC2 reboot which is safer than stop/start.

        Args:
            machine_id: EC2 instance ID

        Returns:
            True if reboot initiated

        Raises:
            CloudNotFoundError: If instance not found
            CloudError: On API errors
        """
        self._ensure_authenticated()

        try:
            log.info(f"Rebooting EC2 instance: {machine_id}")
            self._ec2_client.reboot_instances(InstanceIds=[machine_id])
            log.info(f"Instance {machine_id} reboot initiated")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            raise CloudError(f"Failed to reboot instance {machine_id}: {error_msg}") from e
        except BotoCoreError as e:
            raise CloudError(f"AWS connection error: {e}") from e

    def get_console_output(self, machine_id: str) -> str:
        """Get console output from an EC2 instance.

        Args:
            machine_id: EC2 instance ID

        Returns:
            Console output string (base64 decoded)
        """
        self._ensure_authenticated()

        try:
            response = self._ec2_client.get_console_output(InstanceId=machine_id)

            output = response.get("Output", "")
            if output:
                # Output is base64 encoded
                return base64.b64decode(output).decode("utf-8", errors="replace")
            return ""

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise CloudNotFoundError(f"Instance not found: {machine_id}") from e
            log.warning(f"Failed to get console output for {machine_id}: {e}")
            return ""
        except BotoCoreError as e:
            log.warning(f"Failed to get console output for {machine_id}: {e}")
            return ""

    def list_images(self, filters: dict | None = None) -> list[dict]:
        """List available AMIs.

        Args:
            filters: Optional filters. Supports:
                - owners: List of owner IDs (e.g., ["amazon", "self"])
                - name_pattern: AMI name pattern (e.g., "ubuntu/images/*")
                - architecture: Architecture (e.g., "x86_64", "arm64")
                - image_ids: Specific AMI IDs

        Returns:
            List of AMI info dictionaries
        """
        self._ensure_authenticated()

        describe_kwargs: dict[str, Any] = {}
        ec2_filters: list[dict[str, Any]] = []

        if filters:
            owners = filters.get("owners", [])
            if owners:
                describe_kwargs["Owners"] = owners

            image_ids = filters.get("image_ids", [])
            if image_ids:
                describe_kwargs["ImageIds"] = image_ids

            name_pattern = filters.get("name_pattern")
            if name_pattern:
                ec2_filters.append({"Name": "name", "Values": [name_pattern]})

            architecture = filters.get("architecture")
            if architecture:
                ec2_filters.append({"Name": "architecture", "Values": [architecture]})

            # Only show available images by default
            ec2_filters.append({"Name": "state", "Values": ["available"]})
        else:
            # Default: show Amazon and self-owned images
            describe_kwargs["Owners"] = ["amazon", "self"]
            ec2_filters.append({"Name": "state", "Values": ["available"]})

        if ec2_filters:
            describe_kwargs["Filters"] = ec2_filters

        try:
            images: list[dict] = []
            paginator = self._ec2_client.get_paginator("describe_images")

            for page in paginator.paginate(**describe_kwargs):
                for image in page.get("Images", []):
                    images.append({
                        "id": image.get("ImageId", ""),
                        "name": image.get("Name", ""),
                        "description": image.get("Description", ""),
                        "architecture": image.get("Architecture", ""),
                        "platform": image.get("PlatformDetails", ""),
                        "owner_id": image.get("OwnerId", ""),
                        "creation_date": image.get("CreationDate", ""),
                        "state": image.get("State", ""),
                        "root_device_type": image.get("RootDeviceType", ""),
                        "virtualization_type": image.get("VirtualizationType", ""),
                    })

            return images

        except (ClientError, BotoCoreError) as e:
            log.warning(f"Failed to list images: {e}")
            return []

    def list_sizes(self, filters: dict | None = None) -> list[dict]:
        """List available EC2 instance types.

        Args:
            filters: Optional filters. Supports:
                - instance_types: List of specific instance types
                - vcpus_min: Minimum vCPUs
                - memory_min: Minimum memory in MiB
                - architecture: CPU architecture (e.g., "x86_64", "arm64")

        Returns:
            List of instance type info dictionaries
        """
        self._ensure_authenticated()

        describe_kwargs: dict[str, Any] = {}
        ec2_filters: list[dict[str, Any]] = []

        if filters:
            instance_types = filters.get("instance_types", [])
            if instance_types:
                describe_kwargs["InstanceTypes"] = instance_types

            architecture = filters.get("architecture")
            if architecture:
                ec2_filters.append({
                    "Name": "processor-info.supported-architecture",
                    "Values": [architecture],
                })

        if ec2_filters:
            describe_kwargs["Filters"] = ec2_filters

        try:
            sizes: list[dict] = []
            paginator = self._ec2_client.get_paginator("describe_instance_types")

            for page in paginator.paginate(**describe_kwargs):
                for instance_type in page.get("InstanceTypes", []):
                    vcpus = instance_type.get("VCpuInfo", {}).get("DefaultVCpus", 0)
                    memory_mib = instance_type.get("MemoryInfo", {}).get("SizeInMiB", 0)

                    # Apply min filters
                    if filters:
                        vcpus_min = filters.get("vcpus_min", 0)
                        memory_min = filters.get("memory_min", 0)
                        if vcpus < vcpus_min or memory_mib < memory_min:
                            continue

                    sizes.append({
                        "id": instance_type.get("InstanceType", ""),
                        "name": instance_type.get("InstanceType", ""),
                        "vcpus": vcpus,
                        "memory_mib": memory_mib,
                        "memory_gb": round(memory_mib / 1024, 2),
                        "architectures": instance_type.get(
                            "ProcessorInfo", {}
                        ).get("SupportedArchitectures", []),
                        "network_performance": instance_type.get(
                            "NetworkInfo", {}
                        ).get("NetworkPerformance", ""),
                        "ebs_optimized": instance_type.get(
                            "EbsInfo", {}
                        ).get("EbsOptimizedSupport", "") == "default",
                        "current_generation": instance_type.get(
                            "CurrentGeneration", False
                        ),
                    })

            return sizes

        except (ClientError, BotoCoreError) as e:
            log.warning(f"Failed to list instance types: {e}")
            return []

    def list_regions(self) -> list[dict]:
        """List available AWS regions.

        Returns:
            List of region info dictionaries
        """
        self._ensure_authenticated()

        try:
            response = self._ec2_client.describe_regions()

            regions: list[dict] = []
            for region in response.get("Regions", []):
                regions.append({
                    "id": region.get("RegionName", ""),
                    "name": region.get("RegionName", ""),
                    "endpoint": region.get("Endpoint", ""),
                    "opt_in_status": region.get("OptInStatus", ""),
                })

            return regions

        except (ClientError, BotoCoreError) as e:
            log.warning(f"Failed to list regions: {e}")
            return []

    def list_vpcs(self, filters: dict | None = None) -> list[dict]:
        """List VPCs in the region.

        Args:
            filters: Optional filters

        Returns:
            List of VPC info dictionaries
        """
        self._ensure_authenticated()

        try:
            response = self._ec2_client.describe_vpcs()

            vpcs: list[dict] = []
            for vpc in response.get("Vpcs", []):
                # Get name from tags
                name = ""
                for tag in vpc.get("Tags", []):
                    if tag.get("Key") == "Name":
                        name = tag.get("Value", "")
                        break

                vpcs.append({
                    "id": vpc.get("VpcId", ""),
                    "name": name,
                    "cidr_block": vpc.get("CidrBlock", ""),
                    "is_default": vpc.get("IsDefault", False),
                    "state": vpc.get("State", ""),
                })

            return vpcs

        except (ClientError, BotoCoreError) as e:
            log.warning(f"Failed to list VPCs: {e}")
            return []

    def list_subnets(self, vpc_id: str | None = None) -> list[dict]:
        """List subnets, optionally filtered by VPC.

        Args:
            vpc_id: Optional VPC ID to filter by

        Returns:
            List of subnet info dictionaries
        """
        self._ensure_authenticated()

        describe_kwargs: dict[str, Any] = {}
        if vpc_id:
            describe_kwargs["Filters"] = [{"Name": "vpc-id", "Values": [vpc_id]}]

        try:
            response = self._ec2_client.describe_subnets(**describe_kwargs)

            subnets: list[dict] = []
            for subnet in response.get("Subnets", []):
                # Get name from tags
                name = ""
                for tag in subnet.get("Tags", []):
                    if tag.get("Key") == "Name":
                        name = tag.get("Value", "")
                        break

                subnets.append({
                    "id": subnet.get("SubnetId", ""),
                    "name": name,
                    "vpc_id": subnet.get("VpcId", ""),
                    "cidr_block": subnet.get("CidrBlock", ""),
                    "availability_zone": subnet.get("AvailabilityZone", ""),
                    "available_ip_count": subnet.get("AvailableIpAddressCount", 0),
                    "default_for_az": subnet.get("DefaultForAz", False),
                    "map_public_ip_on_launch": subnet.get("MapPublicIpOnLaunch", False),
                })

            return subnets

        except (ClientError, BotoCoreError) as e:
            log.warning(f"Failed to list subnets: {e}")
            return []

    def list_security_groups(self, vpc_id: str | None = None) -> list[dict]:
        """List security groups, optionally filtered by VPC.

        Args:
            vpc_id: Optional VPC ID to filter by

        Returns:
            List of security group info dictionaries
        """
        self._ensure_authenticated()

        describe_kwargs: dict[str, Any] = {}
        if vpc_id:
            describe_kwargs["Filters"] = [{"Name": "vpc-id", "Values": [vpc_id]}]

        try:
            response = self._ec2_client.describe_security_groups(**describe_kwargs)

            security_groups: list[dict] = []
            for sg in response.get("SecurityGroups", []):
                security_groups.append({
                    "id": sg.get("GroupId", ""),
                    "name": sg.get("GroupName", ""),
                    "description": sg.get("Description", ""),
                    "vpc_id": sg.get("VpcId", ""),
                    "ingress_rules_count": len(sg.get("IpPermissions", [])),
                    "egress_rules_count": len(sg.get("IpPermissionsEgress", [])),
                })

            return security_groups

        except (ClientError, BotoCoreError) as e:
            log.warning(f"Failed to list security groups: {e}")
            return []

    def list_key_pairs(self) -> list[dict]:
        """List SSH key pairs.

        Returns:
            List of key pair info dictionaries
        """
        self._ensure_authenticated()

        try:
            response = self._ec2_client.describe_key_pairs()

            key_pairs: list[dict] = []
            for kp in response.get("KeyPairs", []):
                key_pairs.append({
                    "name": kp.get("KeyName", ""),
                    "fingerprint": kp.get("KeyFingerprint", ""),
                    "key_type": kp.get("KeyType", ""),
                    "created_at": kp.get("CreateTime"),
                })

            return key_pairs

        except (ClientError, BotoCoreError) as e:
            log.warning(f"Failed to list key pairs: {e}")
            return []

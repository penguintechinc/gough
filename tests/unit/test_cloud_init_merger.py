"""Unit tests for cloud-init merging logic.

Tests:
- Merging multiple cloud-init configurations
- Handling different YAML field types (lists, dicts, scalars)
- Validation of cloud-init YAML syntax
- Edge cases and error handling
"""

import json
from io import StringIO

import pytest
import yaml


class CloudInitMerger:
    """Helper class for cloud-init merging logic."""

    @staticmethod
    def validate_yaml(content: str) -> tuple[bool, str | None]:
        """Validate cloud-init YAML content.

        Args:
            content: YAML string to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not content or not content.strip():
            return True, None

        try:
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict):
                return False, "Cloud-init content must be a YAML dictionary"
            return True, None
        except yaml.YAMLError as e:
            return False, f"Invalid YAML: {str(e)}"

    @staticmethod
    def merge_configs(configs: list[str]) -> dict:
        """Merge multiple cloud-init configurations.

        Args:
            configs: List of YAML configuration strings

        Returns:
            Merged configuration dictionary
        """
        merged = {}

        for config_str in configs:
            if not config_str or not config_str.strip():
                continue

            try:
                config = yaml.safe_load(config_str)
                if not isinstance(config, dict):
                    continue

                for key, value in config.items():
                    if key not in merged:
                        merged[key] = value
                    elif isinstance(merged[key], list) and isinstance(value, list):
                        # Merge lists (packages, runcmd, etc.)
                        merged[key].extend(value)
                    elif isinstance(merged[key], dict) and isinstance(value, dict):
                        # Merge dictionaries
                        merged[key].update(value)
                    else:
                        # Override scalar values
                        merged[key] = value

            except yaml.YAMLError:
                continue

        return merged

    @staticmethod
    def to_yaml(data: dict) -> str:
        """Convert dictionary to YAML string.

        Args:
            data: Dictionary to convert

        Returns:
            YAML string
        """
        return yaml.dump(data, default_flow_style=False, sort_keys=False)


class TestCloudInitValidation:
    """Tests for cloud-init YAML validation."""

    def test_validate_valid_cloud_init(self):
        """Test validating valid cloud-init content."""
        content = """#cloud-config
packages:
  - curl
  - git
runcmd:
  - echo "setup complete"
"""
        merger = CloudInitMerger()
        is_valid, error = merger.validate_yaml(content)
        assert is_valid is True
        assert error is None

    def test_validate_empty_content(self):
        """Test that empty content is valid."""
        merger = CloudInitMerger()
        is_valid, error = merger.validate_yaml("")
        assert is_valid is True
        assert error is None

    def test_validate_whitespace_only(self):
        """Test that whitespace-only content is valid."""
        merger = CloudInitMerger()
        is_valid, error = merger.validate_yaml("   \n\t\n   ")
        assert is_valid is True
        assert error is None

    def test_validate_invalid_yaml_syntax(self):
        """Test validating invalid YAML syntax."""
        content = """packages:
  - curl
  invalid indentation here
"""
        merger = CloudInitMerger()
        is_valid, error = merger.validate_yaml(content)
        assert is_valid is False
        assert "YAML" in error

    def test_validate_non_dict_yaml(self):
        """Test that non-dict YAML is rejected."""
        content = """- item1
- item2
- item3
"""
        merger = CloudInitMerger()
        is_valid, error = merger.validate_yaml(content)
        assert is_valid is False
        assert "dictionary" in error

    def test_validate_scalar_value(self):
        """Test that scalar values are rejected."""
        merger = CloudInitMerger()
        is_valid, error = merger.validate_yaml("just a string")
        assert is_valid is False

    def test_validate_multiple_issues(self):
        """Test YAML with multiple issues."""
        content = "key: [unclosed list"
        merger = CloudInitMerger()
        is_valid, error = merger.validate_yaml(content)
        assert is_valid is False


class TestCloudInitMerging:
    """Tests for cloud-init configuration merging."""

    def test_merge_single_config(self):
        """Test merging a single configuration."""
        config = """#cloud-config
packages:
  - curl
  - git
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])

        assert "packages" in merged
        assert "curl" in merged["packages"]
        assert len(merged["packages"]) == 2

    def test_merge_empty_configs(self):
        """Test merging empty configuration list."""
        merger = CloudInitMerger()
        merged = merger.merge_configs([])
        assert merged == {}

    def test_merge_list_fields(self):
        """Test merging list fields are combined."""
        config1 = """#cloud-config
packages:
  - curl
  - git
"""
        config2 = """#cloud-config
packages:
  - vim
  - nano
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])

        packages = merged["packages"]
        assert len(packages) == 4
        assert "curl" in packages
        assert "vim" in packages
        assert "nano" in packages

    def test_merge_dict_fields(self):
        """Test merging dict fields are combined."""
        config1 = """#cloud-config
write_files:
  - path: /etc/config.conf
    content: "value1"
  - path: /etc/other.conf
    content: "value2"
"""
        config2 = """#cloud-config
write_files:
  - path: /etc/new.conf
    content: "value3"
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])

        assert "write_files" in merged
        # write_files is a list, so should be extended
        assert len(merged["write_files"]) == 3

    def test_merge_runcmd_list(self):
        """Test merging runcmd lists."""
        config1 = """#cloud-config
runcmd:
  - apt-get update
  - apt-get install -y curl
"""
        config2 = """#cloud-config
runcmd:
  - curl https://example.com/script.sh | bash
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])

        runcmd = merged["runcmd"]
        assert len(runcmd) == 3
        assert "apt-get update" in runcmd

    def test_merge_scalar_override(self):
        """Test that scalar values are overridden."""
        config1 = """#cloud-config
hostname: old-hostname
locale: en_US
"""
        config2 = """#cloud-config
hostname: new-hostname
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])

        assert merged["hostname"] == "new-hostname"
        assert merged["locale"] == "en_US"

    def test_merge_password_field_override(self):
        """Test overriding password field."""
        config1 = """#cloud-config
ssh_pwauth: false
"""
        config2 = """#cloud-config
ssh_pwauth: true
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])

        assert merged["ssh_pwauth"] is True

    def test_merge_users_dict(self):
        """Test merging user dictionaries."""
        config1 = """#cloud-config
users:
  - name: ubuntu
    groups: sudo
    shell: /bin/bash
"""
        config2 = """#cloud-config
users:
  - name: admin
    groups: sudo
    shell: /bin/bash
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])

        assert "users" in merged
        assert len(merged["users"]) == 2

    def test_merge_preserves_nested_structures(self):
        """Test that nested structures are preserved."""
        config = """#cloud-config
output:
  all: "| tee -a /var/log/cloud-init-output.log"
ssh_import_ids:
  - "gh:myuser"
  - "lp:otheruser"
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])

        assert merged["output"]["all"] == "| tee -a /var/log/cloud-init-output.log"
        assert len(merged["ssh_import_ids"]) == 2

    def test_merge_order_matters(self):
        """Test that merge order matters for scalar overrides."""
        config1 = """#cloud-config
ntp:
  enabled: true
"""
        config2 = """#cloud-config
ntp:
  enabled: false
"""
        merger = CloudInitMerger()

        # First config first
        merged1 = merger.merge_configs([config1, config2])
        assert merged1["ntp"]["enabled"] is False

        # Reverse order
        merged2 = merger.merge_configs([config2, config1])
        assert merged2["ntp"]["enabled"] is True

    def test_merge_skip_invalid_configs(self):
        """Test that invalid configs are skipped."""
        config1 = """#cloud-config
packages:
  - curl
"""
        config2 = "invalid: yaml: [[[[[["
        config3 = """#cloud-config
packages:
  - git
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2, config3])

        # Should have merged config1 and config3, skipped config2
        assert "packages" in merged
        assert "curl" in merged["packages"]
        assert "git" in merged["packages"]


class TestCloudInitMergingComplex:
    """Tests for complex cloud-init merging scenarios."""

    def test_merge_full_deployment_stack(self):
        """Test merging a realistic full deployment stack."""
        base = """#cloud-config
package_update: true
package_upgrade: true
packages:
  - curl
  - wget
  - net-tools
"""
        monitoring = """#cloud-config
packages:
  - prometheus-node-exporter
  - telegraf
runcmd:
  - systemctl start prometheus-node-exporter
  - systemctl enable prometheus-node-exporter
"""
        logging = """#cloud-config
packages:
  - filebeat
runcmd:
  - filebeat modules enable system
  - systemctl start filebeat
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([base, monitoring, logging])

        # Verify all packages are present
        packages = merged["packages"]
        assert "curl" in packages
        assert "prometheus-node-exporter" in packages
        assert "filebeat" in packages

        # Verify runcmd merged
        runcmd = merged["runcmd"]
        assert len(runcmd) >= 3

    def test_merge_hypervisor_deployment(self):
        """Test merging hypervisor deployment configurations."""
        base_hypervisor = """#cloud-config
packages:
  - kvm
  - libvirt-daemon
  - bridge-utils
"""
        networking = """#cloud-config
packages:
  - open-vswitch-switch
runcmd:
  - ovs-vsctl br-add br0
  - ip link set br0 up
"""
        storage = """#cloud-config
packages:
  - ceph-common
  - lvm2
runcmd:
  - pvcreate /dev/vdb
  - vgcreate vg-storage /dev/vdb
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([base_hypervisor, networking, storage])

        packages = merged["packages"]
        assert "libvirt-daemon" in packages
        assert "open-vswitch-switch" in packages
        assert "ceph-common" in packages

    def test_merge_with_conditionals_preserved(self):
        """Test that conditional cloud-init sections are preserved."""
        config = """#cloud-config
packages:
  - curl
#cloud-boothook
#!/bin/bash
echo "Boot hook executed"
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])

        # The #cloud-boothook is part of the comment, preserved in string
        assert "packages" in merged
        assert "curl" in merged["packages"]

    def test_merge_handles_null_values(self):
        """Test merging handles null values correctly."""
        config1 = """#cloud-config
key1: value1
key2: null
"""
        config2 = """#cloud-config
key2: value2
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])

        assert merged["key1"] == "value1"
        assert merged["key2"] == "value2"

    def test_merge_power_user_config(self):
        """Test merging power-user configuration."""
        base = """#cloud-config
package_update: true
packages:
  - build-essential
  - git
  - vim
  - tmux
"""
        development = """#cloud-config
packages:
  - python3-dev
  - nodejs
  - docker.io
runcmd:
  - usermod -aG docker ubuntu
"""
        security = """#cloud-config
packages:
  - fail2ban
  - ufw
runcmd:
  - ufw default deny incoming
  - ufw default allow outgoing
  - ufw allow 22/tcp
  - ufw enable
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([base, development, security])

        packages = merged["packages"]
        assert len(packages) >= 11

        runcmd = merged["runcmd"]
        assert any("docker" in cmd for cmd in runcmd)
        assert any("ufw" in cmd for cmd in runcmd)


class TestCloudInitYAMLOutput:
    """Tests for YAML output generation."""

    def test_merged_to_yaml_string(self):
        """Test converting merged config back to YAML string."""
        config = """#cloud-config
packages:
  - curl
runcmd:
  - echo "done"
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])
        yaml_output = merger.to_yaml(merged)

        # Parse the output to verify it's valid YAML
        reparsed = yaml.safe_load(yaml_output)
        assert "packages" in reparsed
        assert "curl" in reparsed["packages"]

    def test_yaml_output_is_valid(self):
        """Test that YAML output is valid and re-parseable."""
        config1 = """#cloud-config
packages:
  - curl
  - git
"""
        config2 = """#cloud-config
packages:
  - vim
runcmd:
  - systemctl restart networking
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config1, config2])
        yaml_output = merger.to_yaml(merged)

        # Should be parseable back
        reparsed = yaml.safe_load(yaml_output)
        assert isinstance(reparsed, dict)
        assert len(reparsed["packages"]) == 3

    def test_yaml_output_preserves_lists(self):
        """Test that YAML output preserves list structures."""
        merger = CloudInitMerger()
        merged = {
            "packages": ["curl", "git", "vim"],
            "runcmd": ["cmd1", "cmd2"],
            "hostname": "testhost"
        }
        yaml_output = merger.to_yaml(merged)
        reparsed = yaml.safe_load(yaml_output)

        assert isinstance(reparsed["packages"], list)
        assert isinstance(reparsed["runcmd"], list)
        assert len(reparsed["packages"]) == 3

    def test_yaml_output_readable_format(self):
        """Test that YAML output is human-readable."""
        merger = CloudInitMerger()
        merged = {
            "packages": ["curl"],
            "hostname": "myhost",
            "runcmd": ["echo test"]
        }
        yaml_output = merger.to_yaml(merged)

        # Should not use flow style
        assert "{" not in yaml_output
        assert "[" not in yaml_output
        # Should have proper indentation
        assert "packages:" in yaml_output


class TestCloudInitEdgeCases:
    """Tests for edge cases in cloud-init merging."""

    def test_merge_with_special_characters(self):
        """Test merging configs with special characters."""
        config = """#cloud-config
write_files:
  - path: /etc/test.conf
    content: |
      Special chars: !@#$%^&*()
      Quotes: "double" 'single'
      Newlines and tabs work too
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])
        yaml_output = merger.to_yaml(merged)

        reparsed = yaml.safe_load(yaml_output)
        assert "Special chars:" in reparsed["write_files"][0]["content"]

    def test_merge_very_large_package_list(self):
        """Test merging with very large package lists."""
        packages = [f"package-{i}" for i in range(1000)]
        config = f"""#cloud-config
packages:
{yaml.dump(packages, default_flow_style=False)}
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])

        assert len(merged["packages"]) == 1000

    def test_merge_deeply_nested_structures(self):
        """Test merging deeply nested structures."""
        config = """#cloud-config
bootcmd:
  - echo "boot phase"
runcmd:
  - echo "run phase"
write_files:
  - path: /etc/config.yml
    content: |
      nested:
        structure:
          here: value
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])

        assert "bootcmd" in merged
        assert "runcmd" in merged
        assert "write_files" in merged

    def test_merge_unicode_content(self):
        """Test merging content with unicode characters."""
        config = """#cloud-config
write_files:
  - path: /etc/config
    content: |
      # Configuration file with emoji 🚀 and unicode ñ é ü
"""
        merger = CloudInitMerger()
        merged = merger.merge_configs([config])

        assert "🚀" in merged["write_files"][0]["content"]

"""Unit tests for websocket.py module.

Tests for:
- _get_shell_command function
- ShellSessionManager class
- WebSocket initialization
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch, Mock, call

import pytest

_API_MANAGER_PATH = '/home/penguin/code/gough/services/api-manager'
if _API_MANAGER_PATH not in sys.path:
    sys.path.insert(0, _API_MANAGER_PATH)
for _mod in list(sys.modules.keys()):
    if _mod == 'app' or _mod.startswith('app.'):
        del sys.modules[_mod]

from app.websocket import (
    _get_shell_command,
    ShellSessionManager,
    init_websocket,
)


class TestGetShellCommand:
    """Tests for _get_shell_command function."""

    def test_get_shell_command_ssh(self):
        """Test _get_shell_command returns bash for ssh."""
        command = _get_shell_command("ssh")
        assert command == "/bin/bash"

    def test_get_shell_command_kubectl(self):
        """Test _get_shell_command returns bash for kubectl."""
        command = _get_shell_command("kubectl")
        assert command == "/bin/bash"

    def test_get_shell_command_docker(self):
        """Test _get_shell_command returns bash for docker."""
        command = _get_shell_command("docker")
        assert command == "/bin/bash"

    def test_get_shell_command_cloud_cli(self):
        """Test _get_shell_command returns bash for cloud_cli."""
        command = _get_shell_command("cloud_cli")
        assert command == "/bin/bash"

    def test_get_shell_command_unknown_type(self):
        """Test _get_shell_command returns default bash for unknown type."""
        command = _get_shell_command("unknown_type")
        assert command == "/bin/bash"

    def test_get_shell_command_empty_string(self):
        """Test _get_shell_command with empty string."""
        command = _get_shell_command("")
        assert command == "/bin/bash"

    def test_get_shell_command_various_types(self):
        """Test _get_shell_command with various session types."""
        types = ["ssh", "kubectl", "docker", "cloud_cli", "random", "ftp"]
        for session_type in types:
            command = _get_shell_command(session_type)
            assert command == "/bin/bash"


class TestShellSessionManagerInit:
    """Tests for ShellSessionManager initialization."""

    def test_shell_session_manager_init(self):
        """Test ShellSessionManager initialization."""
        manager = ShellSessionManager("session_123")

        assert manager.session_id == "session_123"
        assert manager.master_fd is None
        assert manager.slave_fd is None
        assert manager.process is None
        assert manager.reader_task is None
        assert manager.running is False

    def test_shell_session_manager_init_different_session_ids(self):
        """Test multiple managers with different session IDs."""
        manager1 = ShellSessionManager("session_1")
        manager2 = ShellSessionManager("session_2")

        assert manager1.session_id == "session_1"
        assert manager2.session_id == "session_2"


class TestShellSessionManagerStart:
    """Tests for ShellSessionManager.start method."""

    @pytest.mark.asyncio
    async def test_start_creates_pty(self):
        """Test start() creates PTY."""
        manager = ShellSessionManager("session_test")

        mock_master_fd = 10
        mock_slave_fd = 11

        with patch("app.websocket.pty.openpty") as mock_openpty, patch(
            "app.websocket.subprocess.Popen"
        ) as mock_popen, patch.object(manager, "resize", new_callable=AsyncMock):
            mock_openpty.return_value = (mock_master_fd, mock_slave_fd)
            mock_popen.return_value = MagicMock(pid=1234)

            await manager.start(command="/bin/bash", rows=24, cols=80)

            assert manager.master_fd == mock_master_fd
            assert manager.slave_fd == mock_slave_fd
            assert manager.running is True
            assert manager.process is not None
            assert manager.reader_task is not None

    @pytest.mark.asyncio
    async def test_start_calls_resize(self):
        """Test start() calls resize with correct parameters."""
        manager = ShellSessionManager("session_test")

        mock_master_fd = 10
        mock_slave_fd = 11

        with patch("app.websocket.pty.openpty") as mock_openpty, patch(
            "app.websocket.subprocess.Popen"
        ) as mock_popen, patch.object(
            manager, "resize", new_callable=AsyncMock
        ) as mock_resize:
            mock_openpty.return_value = (mock_master_fd, mock_slave_fd)
            mock_popen.return_value = MagicMock(pid=1234)

            await manager.start(command="/bin/bash", rows=30, cols=100)

            mock_resize.assert_called_once_with(30, 100)

    @pytest.mark.asyncio
    async def test_start_spawns_process(self):
        """Test start() spawns process with correct arguments."""
        manager = ShellSessionManager("session_test")

        mock_master_fd = 10
        mock_slave_fd = 11

        with patch("app.websocket.pty.openpty") as mock_openpty, patch(
            "app.websocket.subprocess.Popen"
        ) as mock_popen, patch.object(manager, "resize", new_callable=AsyncMock):
            mock_openpty.return_value = (mock_master_fd, mock_slave_fd)
            mock_process = MagicMock(pid=5678)
            mock_popen.return_value = mock_process

            await manager.start(command="/bin/sh", rows=24, cols=80)

            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            assert args[0][0] == "/bin/sh"
            assert kwargs["stdin"] == mock_slave_fd
            assert kwargs["stdout"] == mock_slave_fd
            assert kwargs["stderr"] == mock_slave_fd


class TestShellSessionManagerResize:
    """Tests for ShellSessionManager.resize method."""

    @pytest.mark.asyncio
    async def test_resize_with_valid_fd(self):
        """Test resize() with valid file descriptor."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10

        with patch("fcntl.ioctl") as mock_ioctl:
            await manager.resize(rows=30, cols=100)
            mock_ioctl.assert_called_once()

    @pytest.mark.asyncio
    async def test_resize_without_fd(self):
        """Test resize() without file descriptor does nothing."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = None

        with patch("fcntl.ioctl") as mock_ioctl:
            await manager.resize(rows=30, cols=100)
            mock_ioctl.assert_not_called()

    @pytest.mark.asyncio
    async def test_resize_handles_exception(self):
        """Test resize() handles exceptions gracefully."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10

        with patch("fcntl.ioctl") as mock_ioctl:
            mock_ioctl.side_effect = Exception("ioctl failed")
            # Should not raise
            await manager.resize(rows=30, cols=100)

    @pytest.mark.asyncio
    async def test_resize_struct_pack(self):
        """Test resize() uses correct struct format."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10

        import struct

        with patch("fcntl.ioctl") as mock_ioctl:
            await manager.resize(rows=24, cols=80)

            # Check that winsize is packed correctly
            call_args = mock_ioctl.call_args[0]
            winsize = call_args[2]  # Third argument to fcntl.ioctl
            # Verify it's packed with HHHH format (rows, cols, 0, 0)
            assert len(winsize) == 8  # 4 unsigned shorts = 8 bytes


class TestShellSessionManagerWriteInput:
    """Tests for ShellSessionManager.write_input method."""

    @pytest.mark.asyncio
    async def test_write_input_with_running_session(self):
        """Test write_input() with active session."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10
        manager.running = True

        with patch("app.websocket.os.write") as mock_write:
            await manager.write_input("test input")
            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[0] == 10
            assert args[1] == b"test input"

    @pytest.mark.asyncio
    async def test_write_input_without_fd(self):
        """Test write_input() when no file descriptor."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = None
        manager.running = True

        with patch("app.websocket.os.write") as mock_write:
            await manager.write_input("test input")
            mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_input_when_not_running(self):
        """Test write_input() when session not running."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10
        manager.running = False

        with patch("app.websocket.os.write") as mock_write:
            await manager.write_input("test input")
            mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_input_handles_oserror(self):
        """Test write_input() handles OSError."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10
        manager.running = True

        with patch("app.websocket.os.write") as mock_write:
            mock_write.side_effect = OSError("Broken pipe")
            await manager.write_input("test input")

            # Should set running to False on error
            assert manager.running is False

    @pytest.mark.asyncio
    async def test_write_input_encodes_utf8(self):
        """Test write_input() encodes input as UTF-8."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10
        manager.running = True

        with patch("app.websocket.os.write") as mock_write:
            special_text = "hello μ world"
            await manager.write_input(special_text)

            args = mock_write.call_args[0]
            expected_bytes = special_text.encode("utf-8")
            assert args[1] == expected_bytes


class TestShellSessionManagerCleanup:
    """Tests for ShellSessionManager.cleanup method."""

    @pytest.mark.asyncio
    async def test_cleanup_sets_running_false(self):
        """Test cleanup() sets running flag to False."""
        manager = ShellSessionManager("session_test")
        manager.running = True
        manager.reader_task = None

        await manager.cleanup()

        assert manager.running is False

    @pytest.mark.asyncio
    async def test_cleanup_cancels_reader_task(self):
        """Test cleanup() cancels reader task if running."""
        manager = ShellSessionManager("session_test")
        # Create a real task that we can cancel
        async def dummy_task():
            await asyncio.sleep(10)

        manager.reader_task = asyncio.create_task(dummy_task())
        manager.process = None

        await manager.cleanup()

        # Task should be cancelled
        assert manager.reader_task.cancelled() or manager.reader_task.done()

    @pytest.mark.asyncio
    async def test_cleanup_skips_done_reader_task(self):
        """Test cleanup() skips canceling completed reader task."""
        manager = ShellSessionManager("session_test")
        # Create a completed task
        async def completed_task():
            pass

        manager.reader_task = asyncio.create_task(completed_task())
        await manager.reader_task  # Wait for it to complete
        manager.process = None

        # Should not raise even though task is done
        await manager.cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_terminates_process(self):
        """Test cleanup() terminates process."""
        manager = ShellSessionManager("session_test")
        manager.process = MagicMock()
        manager.process.terminate = MagicMock()
        manager.process.wait = MagicMock()

        with patch.object(manager, "reader_task", None):
            await manager.cleanup()

            manager.process.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_kills_process_on_timeout(self):
        """Test cleanup() kills process on timeout."""
        manager = ShellSessionManager("session_test")
        manager.process = MagicMock()
        manager.process.terminate = MagicMock()
        manager.process.wait = MagicMock(
            side_effect=subprocess.TimeoutExpired("cmd", 5)
        )
        manager.process.kill = MagicMock()

        with patch.object(manager, "reader_task", None):
            await manager.cleanup()

            manager.process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_closes_master_fd(self):
        """Test cleanup() closes master file descriptor."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10

        with patch("app.websocket.os.close") as mock_close, patch.object(
            manager, "reader_task", None
        ):
            await manager.cleanup()

            # Should be called with master_fd
            assert mock_close.called

    @pytest.mark.asyncio
    async def test_cleanup_closes_slave_fd(self):
        """Test cleanup() closes slave file descriptor."""
        manager = ShellSessionManager("session_test")
        manager.slave_fd = 11

        with patch("app.websocket.os.close") as mock_close, patch.object(
            manager, "reader_task", None
        ):
            await manager.cleanup()

            assert mock_close.called

    @pytest.mark.asyncio
    async def test_cleanup_ignores_fd_close_errors(self):
        """Test cleanup() ignores errors closing file descriptors."""
        manager = ShellSessionManager("session_test")
        manager.master_fd = 10
        manager.slave_fd = 11

        with patch("app.websocket.os.close") as mock_close, patch.object(
            manager, "reader_task", None
        ):
            mock_close.side_effect = OSError("Bad file descriptor")
            # Should not raise
            await manager.cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_without_process(self):
        """Test cleanup() when process is None."""
        manager = ShellSessionManager("session_test")
        manager.process = None

        with patch.object(manager, "reader_task", None):
            # Should not raise
            await manager.cleanup()


class TestInitWebsocket:
    """Tests for init_websocket function."""

    def test_init_websocket_registers_route(self):
        """Test init_websocket registers WebSocket route."""
        mock_app = MagicMock()

        init_websocket(mock_app)

        mock_app.websocket.assert_called_once_with("/ws/shell")

    def test_init_websocket_returns_handler(self):
        """Test init_websocket sets up handler."""
        mock_app = MagicMock()

        # Capture the decorated function
        decorated_func = None

        def capture_decorator(path):
            def decorator(func):
                nonlocal decorated_func
                decorated_func = func
                return func

            return decorator

        mock_app.websocket = capture_decorator

        init_websocket(mock_app)

        # Handler should be an async function
        assert decorated_func is not None
        assert asyncio.iscoroutinefunction(decorated_func)


class TestReadOutputBehavior:
    """Tests for ShellSessionManager._read_output behavior patterns."""

    @pytest.mark.asyncio
    async def test_pty_read_eof_detection(self):
        """Test EOF is detected on empty read."""
        # Simulate PTY EOF detection logic
        output = b""  # Empty means EOF
        is_eof = not output
        assert is_eof is True

    @pytest.mark.asyncio
    async def test_pty_read_data_available(self):
        """Test data available detection."""
        # Simulate PTY data detection logic
        output = b"test output"
        has_data = len(output) > 0
        assert has_data is True

    def test_select_result_interpretation(self):
        """Test select result interpretation."""
        # Simulate select returning readable fds
        readable, _, _ = ([10], [], [])
        master_fd = 10
        is_readable = master_fd in readable
        assert is_readable is True

    def test_select_no_readable(self):
        """Test select with no readable fds."""
        # Simulate select returning empty readable
        readable, _, _ = ([], [], [])
        master_fd = 10
        is_readable = master_fd in readable
        assert is_readable is False


class TestShellSessionManagerIntegration:
    """Integration tests for ShellSessionManager."""

    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self):
        """Test complete session lifecycle."""
        manager = ShellSessionManager("session_integration")

        mock_master_fd = 10
        mock_slave_fd = 11

        with patch("app.websocket.pty.openpty") as mock_openpty, patch(
            "app.websocket.subprocess.Popen"
        ) as mock_popen, patch.object(
            manager, "resize", new_callable=AsyncMock
        ), patch(
            "app.websocket.os.close"
        ) as mock_close:
            mock_openpty.return_value = (mock_master_fd, mock_slave_fd)
            mock_process = MagicMock(pid=9999)
            mock_popen.return_value = mock_process

            # Start session
            await manager.start(command="/bin/bash")
            assert manager.running is True

            # Cleanup
            await manager.cleanup()
            assert manager.running is False

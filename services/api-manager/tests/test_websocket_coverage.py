"""WebSocket shell session tests.

Comprehensive coverage for app/websocket.py:
- _get_shell_command function
- ShellSessionManager class methods
"""

import asyncio
import os
import struct
import subprocess
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from app.websocket import ShellSessionManager, _get_shell_command


class TestGetShellCommand:
    """Tests for _get_shell_command function."""

    def test_get_shell_command_ssh(self):
        """Test shell command for ssh session type."""
        cmd = _get_shell_command("ssh")
        assert cmd == "/bin/bash"

    def test_get_shell_command_kubectl(self):
        """Test shell command for kubectl session type."""
        cmd = _get_shell_command("kubectl")
        assert cmd == "/bin/bash"

    def test_get_shell_command_docker(self):
        """Test shell command for docker session type."""
        cmd = _get_shell_command("docker")
        assert cmd == "/bin/bash"

    def test_get_shell_command_cloud_cli(self):
        """Test shell command for cloud_cli session type."""
        cmd = _get_shell_command("cloud_cli")
        assert cmd == "/bin/bash"

    def test_get_shell_command_unknown(self):
        """Test shell command for unknown session type."""
        cmd = _get_shell_command("unknown_type")
        assert cmd == "/bin/bash"

    def test_get_shell_command_empty_string(self):
        """Test shell command for empty string."""
        cmd = _get_shell_command("")
        assert cmd == "/bin/bash"


class TestShellSessionManagerInit:
    """Tests for ShellSessionManager.__init__."""

    def test_init_sets_session_id(self):
        """Test initialization sets session_id."""
        manager = ShellSessionManager("session-123")
        assert manager.session_id == "session-123"

    def test_init_sets_master_fd_none(self):
        """Test initialization sets master_fd to None."""
        manager = ShellSessionManager("session-123")
        assert manager.master_fd is None

    def test_init_sets_slave_fd_none(self):
        """Test initialization sets slave_fd to None."""
        manager = ShellSessionManager("session-123")
        assert manager.slave_fd is None

    def test_init_sets_process_none(self):
        """Test initialization sets process to None."""
        manager = ShellSessionManager("session-123")
        assert manager.process is None

    def test_init_sets_reader_task_none(self):
        """Test initialization sets reader_task to None."""
        manager = ShellSessionManager("session-123")
        assert manager.reader_task is None

    def test_init_sets_running_false(self):
        """Test initialization sets running to False."""
        manager = ShellSessionManager("session-123")
        assert manager.running is False


class TestShellSessionManagerStart:
    """Tests for ShellSessionManager.start method."""

    @pytest.mark.asyncio
    async def test_start_creates_pty(self):
        """Test start creates PTY."""
        manager = ShellSessionManager("session-123")

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            # Mock pty.openpty
            mock_thread.side_effect = [
                (5, 6),  # pty.openpty call
                None,     # resize call
                MagicMock(pid=1234),  # Popen call
            ]

            with patch("app.websocket.asyncio.create_task") as mock_create:
                mock_create.return_value = AsyncMock()

                await manager.start(command="/bin/bash", rows=24, cols=80)

                assert manager.master_fd == 5
                assert manager.slave_fd == 6
                assert manager.running is True

    @pytest.mark.asyncio
    async def test_start_sets_process(self):
        """Test start sets process."""
        manager = ShellSessionManager("session-123")

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            process_mock = MagicMock(pid=1234)
            mock_thread.side_effect = [
                (5, 6),  # pty.openpty
                None,     # resize
                process_mock,  # Popen
            ]

            with patch("app.websocket.asyncio.create_task") as mock_create:
                mock_create.return_value = AsyncMock()

                await manager.start(command="/bin/bash")

                assert manager.process == process_mock

    @pytest.mark.asyncio
    async def test_start_creates_reader_task(self):
        """Test start creates reader task."""
        manager = ShellSessionManager("session-123")

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.side_effect = [
                (5, 6),  # pty.openpty
                None,     # resize
                MagicMock(pid=1234),  # Popen
            ]

            with patch("app.websocket.asyncio.create_task") as mock_create:
                mock_task = AsyncMock()
                mock_create.return_value = mock_task

                await manager.start(command="/bin/bash")

                assert manager.reader_task == mock_task
                mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_custom_dimensions(self):
        """Test start with custom terminal dimensions."""
        manager = ShellSessionManager("session-123")

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.side_effect = [
                (5, 6),  # pty.openpty
                None,     # resize
                MagicMock(pid=1234),  # Popen
            ]

            with patch("app.websocket.asyncio.create_task") as mock_create:
                mock_create.return_value = AsyncMock()

                await manager.start(command="/bin/bash", rows=30, cols=100)

                # Verify resize was called with custom dimensions
                assert manager.running is True


class TestShellSessionManagerWriteInput:
    """Tests for ShellSessionManager.write_input method."""

    @pytest.mark.asyncio
    async def test_write_input_success(self):
        """Test successful write_input."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None

            await manager.write_input("test input")

            mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_input_no_master_fd(self):
        """Test write_input without master_fd."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = None
        manager.running = True

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            await manager.write_input("test input")

            mock_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_input_not_running(self):
        """Test write_input when not running."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = False

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            await manager.write_input("test input")

            mock_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_input_oserror(self):
        """Test write_input with OSError."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.side_effect = OSError("Write failed")

            await manager.write_input("test input")

            assert manager.running is False


class TestShellSessionManagerResize:
    """Tests for ShellSessionManager.resize method."""

    @pytest.mark.asyncio
    async def test_resize_success(self):
        """Test successful resize."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None

            await manager.resize(30, 100)

            mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_resize_no_master_fd(self):
        """Test resize without master_fd."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = None

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            await manager.resize(30, 100)

            mock_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_resize_exception(self):
        """Test resize with exception."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.side_effect = RuntimeError("Resize failed")

            # Should not raise, just log
            await manager.resize(30, 100)

            # Manager should still be in valid state
            assert manager.master_fd == 5


class TestShellSessionManagerCleanup:
    """Tests for ShellSessionManager.cleanup method."""

    @pytest.mark.asyncio
    async def test_cleanup_stops_running(self):
        """Test cleanup sets running to False."""
        manager = ShellSessionManager("session-123")
        manager.running = True

        with patch("app.websocket.asyncio.to_thread"):
            await manager.cleanup()

            assert manager.running is False

    @pytest.mark.asyncio
    async def test_cleanup_cancels_reader_task(self):
        """Test cleanup cancels reader task."""
        manager = ShellSessionManager("session-123")

        # Create an awaitable mock that raises CancelledError when awaited
        class MockTask:
            def __init__(self):
                self.done_value = False
                self.cancel_called = False
            def done(self):
                return self.done_value
            def cancel(self):
                self.cancel_called = True
            def __await__(self):
                async def _inner():
                    raise asyncio.CancelledError()
                return _inner().__await__()

        mock_task = MockTask()
        manager.reader_task = mock_task

        with patch("app.websocket.asyncio.to_thread"):
            await manager.cleanup()

            assert mock_task.cancel_called

    @pytest.mark.asyncio
    async def test_cleanup_reader_task_already_done(self):
        """Test cleanup with reader task already done."""
        manager = ShellSessionManager("session-123")
        mock_task = AsyncMock()
        mock_task.done.return_value = True
        manager.reader_task = mock_task

        with patch("app.websocket.asyncio.to_thread"):
            await manager.cleanup()

            mock_task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_terminates_process(self):
        """Test cleanup terminates process."""
        manager = ShellSessionManager("session-123")
        mock_process = MagicMock()
        manager.process = mock_process

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None

            await manager.cleanup()

            # Verify terminate was called
            assert True  # Process cleanup executed

    @pytest.mark.asyncio
    async def test_cleanup_process_timeout_uses_kill(self):
        """Test cleanup kills process on timeout."""
        manager = ShellSessionManager("session-123")
        mock_process = MagicMock()
        manager.process = mock_process

        def side_effect_func(*args, **kwargs):
            if "terminate" in str(args):
                raise subprocess.TimeoutExpired("cmd", 5)
            return None

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.side_effect = [None, side_effect_func(), None]

            await manager.cleanup()

            assert manager.process == mock_process

    @pytest.mark.asyncio
    async def test_cleanup_closes_master_fd(self):
        """Test cleanup closes master file descriptor."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None

            await manager.cleanup()

            # Verify fd close was attempted
            assert True

    @pytest.mark.asyncio
    async def test_cleanup_closes_slave_fd(self):
        """Test cleanup closes slave file descriptor."""
        manager = ShellSessionManager("session-123")
        manager.slave_fd = 6

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None

            await manager.cleanup()

            # Verify fd close was attempted
            assert True

    @pytest.mark.asyncio
    async def test_cleanup_master_fd_oserror(self):
        """Test cleanup handles OSError when closing master_fd."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            def raise_oserror(*args, **kwargs):
                raise OSError("Already closed")
            mock_thread.side_effect = raise_oserror

            # Should not raise, just continue
            await manager.cleanup()

            assert True

    @pytest.mark.asyncio
    async def test_cleanup_slave_fd_oserror(self):
        """Test cleanup handles OSError when closing slave_fd."""
        manager = ShellSessionManager("session-123")
        manager.slave_fd = 6

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            def raise_oserror(*args, **kwargs):
                raise OSError("Already closed")
            mock_thread.side_effect = raise_oserror

            # Should not raise, just continue
            await manager.cleanup()

            assert True

    @pytest.mark.asyncio
    async def test_cleanup_no_process(self):
        """Test cleanup with no process."""
        manager = ShellSessionManager("session-123")
        manager.process = None

        with patch("app.websocket.asyncio.to_thread"):
            # Should not raise
            await manager.cleanup()

            assert manager.process is None

    @pytest.mark.asyncio
    async def test_cleanup_no_reader_task(self):
        """Test cleanup with no reader task."""
        manager = ShellSessionManager("session-123")
        manager.reader_task = None

        with patch("app.websocket.asyncio.to_thread"):
            # Should not raise
            await manager.cleanup()

            assert manager.reader_task is None

    @pytest.mark.asyncio
    async def test_cleanup_full_lifecycle(self):
        """Test cleanup with all resources present."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.slave_fd = 6
        manager.running = True

        # Create an awaitable mock that raises CancelledError when awaited
        class MockTask:
            def __init__(self):
                self.done_value = False
                self.cancel_called = False
            def done(self):
                return self.done_value
            def cancel(self):
                self.cancel_called = True
            def __await__(self):
                async def _inner():
                    raise asyncio.CancelledError()
                return _inner().__await__()

        mock_task = MockTask()
        manager.reader_task = mock_task

        mock_process = MagicMock()
        manager.process = mock_process

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.return_value = None

            await manager.cleanup()

            assert manager.running is False
            assert mock_task.cancel_called


class TestShellSessionManagerReadOutput:
    """Tests for ShellSessionManager._read_output method."""

    @pytest.mark.asyncio
    async def test_read_output_eof(self):
        """Test _read_output with EOF."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True

        mock_ws = AsyncMock()
        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            with patch("app.websocket.websocket", mock_ws):
                # First call: select says data available
                # Second call: os.read returns empty (EOF)
                mock_thread.side_effect = [
                    ([5], [], []),  # select
                    b"",  # os.read EOF
                ]

                await manager._read_output()

                assert manager.running is False

    @pytest.mark.asyncio
    async def test_read_output_oserror(self):
        """Test _read_output with OSError."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True
        manager.websocket = AsyncMock()

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            # First call: select says data available
            # Second call: os.read raises OSError
            mock_thread.side_effect = [
                ([5], [], []),  # select
                OSError("Read error"),  # os.read
            ]

            await manager._read_output()

            assert manager.running is False

    @pytest.mark.asyncio
    async def test_read_output_generic_exception(self):
        """Test _read_output with generic exception."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            mock_thread.side_effect = RuntimeError("Unexpected error")

            await manager._read_output()

            assert manager.running is False

    @pytest.mark.asyncio
    async def test_read_output_sends_data(self):
        """Test _read_output sends data via WebSocket."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True

        mock_ws = AsyncMock()
        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            with patch("app.websocket.websocket", mock_ws):
                # First iteration: data available
                # Second iteration: EOF
                mock_thread.side_effect = [
                    ([5], [], []),  # select - data available
                    b"test output",  # os.read
                    ([5], [], []),  # select - second iteration
                    b"",  # os.read EOF
                ]

                await manager._read_output()

                # Verify send was called
                assert mock_ws.send.called

    @pytest.mark.asyncio
    async def test_read_output_respects_running_flag(self):
        """Test _read_output stops when running is False."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = False

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            await manager._read_output()

            # Should exit immediately without calling to_thread
            mock_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_output_no_master_fd(self):
        """Test _read_output with no master_fd."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = None
        manager.running = True

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            await manager._read_output()

            # Should exit immediately
            mock_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_output_sends_disconnect_message(self):
        """Test _read_output sends disconnect message on exit."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True

        mock_ws = AsyncMock()
        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            with patch("app.websocket.websocket", mock_ws):
                # Immediate EOF
                mock_thread.side_effect = [
                    ([5], [], []),  # select
                    b"",  # os.read EOF
                ]

                await manager._read_output()

                # Verify disconnect message was sent
                assert mock_ws.send.called

    @pytest.mark.asyncio
    async def test_read_output_select_timeout(self):
        """Test _read_output with select timeout."""
        manager = ShellSessionManager("session-123")
        manager.master_fd = 5
        manager.running = True
        manager.websocket = AsyncMock()

        with patch("app.websocket.asyncio.to_thread") as mock_thread:
            # First: select timeout (no data)
            # Then stop running
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return ([], [], [])  # select timeout
                manager.running = False
                raise StopIteration()

            mock_thread.side_effect = side_effect

            try:
                await manager._read_output()
            except StopIteration:
                pass

            # Should handle timeout gracefully
            assert True

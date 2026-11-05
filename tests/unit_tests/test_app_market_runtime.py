"""Exercise real static app processes and failed-launch cleanup."""
import asyncio
import json
import sys
import urllib.request
import urllib.error
from urllib.parse import urlsplit

import psutil
import pytest

from jiuwenswarm.extensions.app_market.manager import AppMarketManager
from jiuwenswarm.extensions.app_market.app_market.models import AppManifest, platform_key
from jiuwenswarm.extensions.app_market.app_market.skill_adapter import SkillAdapter
from jiuwenswarm.extensions.app_market.app_market.process_supervisor import ProcessSupervisor


def test_static_app_start_open_stop_and_restart(tmp_path):
    async def scenario():
        page = tmp_path / "gomoku.html"
        page.write_text('<html><meta name="description" content="Play five in a row"><body>gomoku</body></html>')
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "session")
        for _ in range(2):
            result = await manager.start_app(app["app_id"])
            try:
                assert result and result["state"] == "running"
                assert result["url"].endswith("/gomoku.html")
                assert b"gomoku" in urllib.request.urlopen(result["url"]).read()
                web_url = urlsplit(result["web_url"])
                assert web_url.hostname.startswith("workswarm-app-") and web_url.hostname.endswith(".localhost")
                assert web_url.path == "/gomoku.html"
                assert web_url.port == result["port"]
                active = next(item for item in manager.list_instances()["instances"] if item["state"] == "running")
                assert active["web_url"] == result["web_url"]
                instance = manager._service.get_instance(result["instance_id"])
                status_url = f"http://127.0.0.1:{instance.port}/_workswarm/status"
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(status_url)
                assert error.value.code == 401
                request = urllib.request.Request(status_url, headers={"Authorization": f"Bearer {instance.status_token}"})
                status = json.load(urllib.request.urlopen(request))
                assert status["instance_id"] == instance.instance_id
                assert status["app_id"] == app["app_id"]
            finally:
                await manager.shutdown()
            assert not manager._service._supervisor.is_alive(instance.pid)
            assert not manager._service._port_manager.is_allocated(instance.port)
        assert manager.list_apps()["apps"][0]["description"] == "Play five in a row"
    asyncio.run(scenario())


def test_static_server_accepts_market_web_origin_only(tmp_path):
    async def scenario():
        page = tmp_path / "index.html"
        page.write_text("<html><body>web origin</body></html>")
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "session")
        started = await manager.start_app(app["app_id"])
        assert started and started["state"] == "running"
        web_url = urlsplit(started["web_url"])
        try:
            # The Web container opens the per-app *.localhost origin, so the
            # static server must serve that host while still rejecting others.
            request = urllib.request.Request(started["url"], headers={"Host": web_url.netloc})
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
                assert b"web origin" in response.read()
            foreign = urllib.request.Request(
                started["url"], headers={"Host": f"evil.example:{started['port']}"}
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(foreign)
            assert error.value.code == 403
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_failed_spawn_releases_port_and_records_failure(tmp_path):
    async def scenario():
        manager = AppMarketManager(str(tmp_path / "market"))
        manifest = AppManifest(app_id="broken", name="broken", version="1", run_type="managed",
            start_script={platform_key(): "missing-command"}, work_dir=str(tmp_path), default_port=18991)
        manager._service._storage.save_manifest("broken", manifest)
        assert await manager.start_app("broken") is None
        assert not manager._service._port_manager.is_allocated(18991)
        instance = manager._service.list_instances()[0]
        assert instance.state == "stopped"
        assert instance.detail
    asyncio.run(scenario())


def test_powershell_script_uses_shell_even_with_application_interpreter():
    command = ProcessSupervisor.script_command("scripts/start.ps1", "python", ["arg"])
    assert command == ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "scripts/start.ps1", "arg"]


def test_legacy_static_scripts_are_upgraded_without_overwriting_custom_scripts(tmp_path):
    adapter = SkillAdapter()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    pk = platform_key()
    suffix = "ps1" if pk == "windows" else "sh"
    path = scripts / f"start.{suffix}"
    legacy = adapter._legacy_windows_start if pk == "windows" else adapter._legacy_unix_start
    path.write_text(legacy("static", "pull"), encoding="utf-8")
    manifest = AppManifest(app_id="old", name="old", version="1", run_type="static", work_dir=str(tmp_path),
        start_script={pk: f"scripts/start.{suffix}"})
    adapter.upgrade_static_scripts(manifest)
    assert "WORKSWARM_STATIC_SERVER" in path.read_text()
    path.write_text("custom-server")
    adapter.upgrade_static_scripts(manifest)
    assert path.read_text() == "custom-server"


def test_unconfirmed_readiness_is_failure_and_releases_resources(tmp_path, monkeypatch):
    async def scenario():
        page = tmp_path / "app.html"
        page.write_text("<html>hello</html>")
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "session")
        async def unconfirmed(*args, **kwargs):
            return False
        monkeypatch.setattr(manager._service, "_wait_for_running", unconfirmed)
        assert await manager.start_app(app["app_id"]) is None
        instance = manager._service.list_instances()[0]
        assert instance.state == "stopped"
        assert not manager._service._supervisor.is_alive(instance.pid)
        assert not manager._service._port_manager.is_allocated(instance.port)
        assert manager.start_error(app["app_id"])
    asyncio.run(scenario())


def test_readiness_rejects_another_instance_status(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.app_market.app_market.models import AppInstance, InstanceStatus
    async def scenario():
        manager = AppMarketManager(str(tmp_path / "market"))
        async def unrelated(*args):
            return InstanceStatus(app_id="other", instance_id="other", seq=1, state="running")
        monkeypatch.setattr(manager._service._status_manager, "poll_pull", unrelated)
        instance = AppInstance(instance_id="own", app_id="own", port=1234, status_token="token")
        manifest = AppManifest(app_id="own", name="own", version="1", run_type="static")
        assert not await manager._service._wait_for_running(instance, manifest, timeout=0.01)
    asyncio.run(scenario())


def test_legacy_app_description_falls_back_to_html_title(tmp_path):
    page = tmp_path / "gomoku.html"
    page.write_text("<html><title>Gomoku AI battle</title></html>")
    manager = AppMarketManager(str(tmp_path / "market"))
    manifest = AppManifest(app_id="old", name="gomoku", version="1", run_type="static",
        page_entry_path=page.name, work_dir=str(tmp_path))
    manager._service._storage.save_manifest("old", manifest)
    assert manager.list_apps()["apps"][0]["description"] == "Gomoku AI battle"


def test_market_assigns_non_conflicting_ports_and_injects_gateway_config(tmp_path):
    import socket
    async def run():
        page=tmp_path/"app.html";page.write_text("<html>app</html>")
        manager=AppMarketManager(str(tmp_path/"market"))
        manager._service.configure_gateway(gateway_port=25001,web_port=25000, gateway_host="127.0.0.1")
        app=await manager.publish_app(str(page),"source")
        blocker=socket.socket();blocker.bind(("127.0.0.1",0));blocker.listen()
        manifest=manager._service.get_app(app["app_id"]);manifest.default_port=blocker.getsockname()[1]
        manager._service._storage.save_manifest(manifest.app_id,manifest)
        captured=[]
        original=manager._service._supervisor.start
        async def start(**kwargs):
            captured.append(kwargs["env"])
            return await original(**kwargs)
        manager._service._supervisor.start=start
        try:
            one=await manager.start_app(app["app_id"])
            two=await manager.start_app(app["app_id"])
            assert one and two and one["port"]!=two["port"]!=manifest.default_port
            for env,result in zip(captured,[one,two]):
                assert env["WORKSWARM_APP_PORT"]==str(result["port"])
                assert env["WORKSWARM_APP_HOST"]=="127.0.0.1"
                assert env["WORKSWARM_APP_URL"]==f"http://127.0.0.1:{result['port']}"
                assert env["WORKSWARM_GATEWAY_PORT"]=="25001"
                assert env["WORKSWARM_GATEWAY_API_URL"]=="ws://127.0.0.1:25000/ws"
                assert env["WORKSWARM_GATEWAY_URL"]=="ws://127.0.0.1:25001/tui"
                assert env["WORKSWARM_SOURCE_SESSION"] == "source"
                instance = manager._service.get_instance(result["instance_id"])
                assert manager._service._instance_env(instance)["WORKSWARM_SOURCE_SESSION"] == "source"
        finally:
            blocker.close();await manager.shutdown()
    asyncio.run(run())


def test_market_does_not_invent_gateway_address_when_not_injected(tmp_path):
    manager = AppMarketManager(str(tmp_path / "market"))
    manager._service.configure_gateway(gateway_port=None, web_port=25000, gateway_host=None)
    assert manager._service._gateway_env() == {}
    manager._service.configure_gateway(gateway_port=25001, web_port=25000, gateway_host=None)
    assert manager._service._gateway_env() == {}
    manager._service.configure_gateway(gateway_port=25001, web_port=None, gateway_host="127.0.0.1")
    assert manager._service._gateway_env() == {}


def test_market_reuses_running_instance_for_same_owner_and_app(tmp_path):
    async def run():
        page = tmp_path / "app.html"
        page.write_text("<html>app</html>")
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "source")
        try:
            one = await manager.start_app(app["app_id"], owner_id="owner")
            two = await manager.start_app(app["app_id"], owner_id="owner")
            assert one and two
            assert one["instance_id"] == two["instance_id"]
            assert len(manager._service.list_instances(app["app_id"])) == 1
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_shutdown_endpoint_requires_token_and_stops_the_static_server(tmp_path):
    async def scenario():
        page = tmp_path / "gomoku.html"
        page.write_text("<html><body>gomoku</body></html>")
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "session")
        started = await manager.start_app(app["app_id"])
        assert started and started["state"] == "running"
        instance = manager._service.get_instance(started["instance_id"])
        shutdown_url = f"http://127.0.0.1:{instance.port}/_workswarm/shutdown"
        try:
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(shutdown_url, method="POST"))
            assert error.value.code == 401
            request = urllib.request.Request(shutdown_url, headers={"Authorization": f"Bearer {instance.status_token}"}, method="POST")
            with urllib.request.urlopen(request) as resp:
                assert resp.status == 200
            # The server exits by itself after accepting the shutdown request;
            # no stop_app/force-kill is involved up to this point.
            await asyncio.sleep(2)
            assert not manager._service._supervisor.is_alive(instance.pid)
            assert await manager.stop_app(instance.instance_id)
            assert manager._service.get_instance(instance.instance_id).state == "stopped"
            assert not manager._service._port_manager.is_allocated(instance.port)
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_stop_app_requests_graceful_shutdown_before_killing(tmp_path):
    async def scenario():
        page = tmp_path / "gomoku.html"
        page.write_text("<html><body>gomoku</body></html>")
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "session")
        started = await manager.start_app(app["app_id"])
        instance = manager._service.get_instance(started["instance_id"])
        # Hold the Popen so the exit code survives supervisor cleanup tracking.
        popen = manager._service._supervisor._processes[instance.pid]
        assert await manager.stop_app(instance.instance_id)
        # Exit code 0 proves the wrapper exited by itself (graceful); a
        # TerminateJobObject/taskkill force-kill would exit with code 1.
        assert popen.poll() == 0
        assert not manager._service._supervisor.is_alive(instance.pid)
    asyncio.run(scenario())


def test_stop_app_falls_back_to_force_kill_without_shutdown_endpoint(tmp_path, monkeypatch):
    async def scenario():
        page = tmp_path / "gomoku.html"
        page.write_text("<html><body>gomoku</body></html>")
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "session")
        started = await manager.start_app(app["app_id"])
        instance = manager._service.get_instance(started["instance_id"])
        popen = manager._service._supervisor._processes[instance.pid]
        calls = []

        async def unavailable(port, token):
            calls.append((port, token))
            return False

        monkeypatch.setattr(manager._service._status_manager, "request_shutdown", unavailable)
        assert await manager.stop_app(instance.instance_id)
        assert calls == [(instance.port, instance.status_token)]
        assert not manager._service._supervisor.is_alive(instance.pid)
        assert popen.poll() not in (None, 0)
        assert not manager._service._port_manager.is_allocated(instance.port)
        assert manager._service.get_instance(instance.instance_id).state == "stopped"
    asyncio.run(scenario())


def test_static_pages_are_served_uncached(tmp_path):
    async def scenario():
        # The market reuses ports across apps (both default to 8080); a
        # heuristically cached page would render the previous app under the
        # new app's URL, so every response must forbid caching.
        page = tmp_path / "gomoku.html"
        page.write_text("<html><body>gomoku</body></html>")
        manager = AppMarketManager(str(tmp_path / "market"))
        app = await manager.publish_app(str(page), "session")
        started = await manager.start_app(app["app_id"])
        assert started and started["state"] == "running"
        try:
            with urllib.request.urlopen(started["url"]) as resp:
                assert resp.status == 200
                assert resp.headers.get("Cache-Control") == "no-store"
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("disable_job", [False, True])
def test_graceful_stop_reclaims_children_after_parent_exits(tmp_path, monkeypatch, disable_job):
    from jiuwenswarm.extensions.app_market.app_market import process_supervisor

    if disable_job:
        if sys.platform != "win32":
            pytest.skip("The non-Job fallback is Windows-specific")
        monkeypatch.setattr(process_supervisor, "_HAS_WIN32JOB", False)

    async def scenario():
        page = tmp_path / "index.html"
        page.write_text("<html>review</html>")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "app_server.py").write_text(
            "import os, pathlib, runpy, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "pathlib.Path('child.pid').write_text(str(child.pid))\n"
            "runpy.run_path(os.environ['WORKSWARM_STATIC_SERVER'], run_name='__main__')\n"
        )
        manager = AppMarketManager(str(tmp_path / "market"))
        child = None
        try:
            app = await manager.publish_app(str(page), "session")
            started = await manager.start_app(app["app_id"])
            assert started and started["state"] == "running"
            instance = manager._service.get_instance(started["instance_id"])
            popen = manager._service._supervisor._processes[instance.pid]
            child = psutil.Process(int((tmp_path / "child.pid").read_text()))
            assert child.is_running()
            assert await manager.stop_app(instance.instance_id)
            assert popen.poll() == 0
            assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
            assert manager._service.get_instance(instance.instance_id).state == "stopped"
            assert not manager._service._port_manager.is_allocated(instance.port)
        finally:
            if child is not None and child.is_running():
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            await manager.shutdown()

    asyncio.run(scenario())

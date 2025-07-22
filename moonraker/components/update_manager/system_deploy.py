# Provides System Package Updates
#
# Copyright (C) 2023  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import logging
import time
import re
from .base_deploy import BaseDeploy

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Optional,
    Union,
    Dict,
    List,
)

if TYPE_CHECKING:
    from ...confighelper import ConfigHelper
    from ..shell_command import ShellCommandFactory as SCMDComp
    from ..machine import Machine
    from .update_manager import CommandHelper
    JsonType = Union[List[Any], Dict[str, Any]]


class PackageDeploy(BaseDeploy):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, "system", "", "")
        self.cmd_helper.set_package_updater(self)
        self.available_packages: List[str] = []

    async def initialize(self) -> Dict[str, Any]:
        storage = await super().initialize()
        self.available_packages = storage.get('packages', [])
        provider: BasePackageProvider
        try_fallback = True
        if try_fallback:
            # Check to see of the apt command is available
            fallback = await self._get_fallback_provider()
            if fallback is None:
                provider = BasePackageProvider(self.cmd_helper)
                machine: Machine = self.server.lookup_component("machine")
                dist_info = machine.get_system_info()['distribution']
                dist_id: str = dist_info['id'].lower()
                self.server.add_warning(
                    "Unable to initialize System Update Provider for "
                    f"distribution: {dist_id}")
            else:
                self.log_info("PackageDeploy: Using APT CLI Provider")
                self.prefix = "Package Manager APT: "
                provider = fallback
        self.provider = provider  # type: ignore
        return storage

    async def _get_fallback_provider(self) -> Optional[BasePackageProvider]:
        # Currently only the API Fallback provider is available
        shell_cmd: SCMDComp
        shell_cmd = self.server.lookup_component("shell_command")
        cmd = shell_cmd.build_shell_command("sh -c 'command -v apt'")
        try:
            ret = await cmd.run_with_response()
        except shell_cmd.error:
            return None
        # APT Command found should be available
        self.log_debug(f"APT package manager detected: {ret}")
        provider = AptCliProvider(self.cmd_helper)
        try:
            await provider.initialize()
        except Exception:
            return None
        return provider

    async def refresh(self) -> None:
        try:
            # Do not force a refresh until the server has started
            if self.server.is_running():
                await self._update_package_cache(force=True)
            self.available_packages = await self.provider.get_packages()
            pkg_msg = "\n".join(self.available_packages)
            self.log_info(
                f"Detected {len(self.available_packages)} package updates:"
                f"\n{pkg_msg}"
            )
        except Exception:
            self.log_exc("Error Refreshing System Packages")
        # Update Persistent Storage
        self._save_state()

    def get_persistent_data(self) -> Dict[str, Any]:
        storage = super().get_persistent_data()
        storage['packages'] = self.available_packages
        return storage

    async def update(self) -> bool:
        if not self.available_packages:
            return False
        self.cmd_helper.notify_update_response("Updating packages...")
        try:
            await self._update_package_cache(force=True, notify=True)
            await self.provider.upgrade_system()
        except Exception:
            raise self.server.error("Error updating system packages")
        self.available_packages = []
        self._save_state()
        self.cmd_helper.notify_update_response(
            "Package update finished...", is_complete=True)
        return True

    async def _update_package_cache(self,
                                    force: bool = False,
                                    notify: bool = False
                                    ) -> None:
        curtime = time.time()
        if force or curtime > self.last_refresh_time + 3600.:
            # Don't update if a request was done within the last hour
            await self.provider.refresh_packages(notify)

    async def install_packages(self,
                               package_list: List[str],
                               **kwargs
                               ) -> None:
        await self.provider.install_packages(package_list, **kwargs)

    def get_update_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "configured_type": "system",
            "package_count": len(self.available_packages),
            "package_list": self.available_packages
        }

class BasePackageProvider:
    def __init__(self, cmd_helper: CommandHelper) -> None:
        self.server = cmd_helper.get_server()
        self.cmd_helper = cmd_helper

    async def initialize(self) -> None:
        pass

    async def refresh_packages(self, notify: bool = False) -> None:
        raise self.server.error("Cannot refresh packages, no provider set")

    async def get_packages(self) -> List[str]:
        raise self.server.error("Cannot retrieve packages, no provider set")

    async def install_packages(self,
                               package_list: List[str],
                               **kwargs
                               ) -> None:
        raise self.server.error("Cannot install packages, no provider set")

    async def upgrade_system(self) -> None:
        raise self.server.error("Cannot upgrade packages, no provider set")

class AptCliProvider(BasePackageProvider):
    APT_CMD = "sudo DEBIAN_FRONTEND=noninteractive apt-get"

    async def refresh_packages(self, notify: bool = False) -> None:
        await self.cmd_helper.run_cmd(
            f"{self.APT_CMD} update", timeout=600., notify=notify)

    async def get_packages(self) -> List[str]:
        shell_cmd = self.cmd_helper.get_shell_command()
        res = await shell_cmd.exec_cmd("apt list --upgradable", timeout=60.)
        pkg_list = [p.strip() for p in res.split("\n") if p.strip()]
        if pkg_list:
            pkg_list = pkg_list[2:]
            return [p.split("/", maxsplit=1)[0] for p in pkg_list]
        return []

    async def resolve_packages(self, package_list: List[str]) -> List[str]:
        self.cmd_helper.notify_update_response("Resolving packages...")
        search_regex = "|".join([f"^{pkg}$" for pkg in package_list])
        cmd = f"apt-cache search --names-only \"{search_regex}\""
        shell_cmd = self.cmd_helper.get_shell_command()
        ret = await shell_cmd.exec_cmd(cmd, timeout=600.)
        resolved = [
            pkg.strip().split()[0] for pkg in ret.split("\n") if pkg.strip()
        ]
        return [avail for avail in package_list if avail in resolved]

    async def install_packages(self,
                               package_list: List[str],
                               **kwargs
                               ) -> None:
        timeout: float = kwargs.get('timeout', 300.)
        retries: int = kwargs.get('retries', 3)
        notify: bool = kwargs.get('notify', False)
        await self.refresh_packages(notify=notify)
        resolved = await self.resolve_packages(package_list)
        if not resolved:
            self.cmd_helper.notify_update_response("No packages detected")
            return
        logging.debug(f"Resolved packages: {resolved}")
        pkgs = " ".join(resolved)
        await self.cmd_helper.run_cmd(
            f"{self.APT_CMD} install --yes {pkgs}", timeout=timeout,
            attempts=retries, notify=notify)

    async def upgrade_system(self) -> None:
        await self.cmd_helper.run_cmd(
            f"{self.APT_CMD} upgrade --yes", timeout=3600.,
            notify=True)


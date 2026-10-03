#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import dataclass, is_dataclass
import textwrap
import datetime
import json
from typing import Sequence
import tempfile


@dataclass
class OsVersion:
    os_id: str
    os_version_id: str

    @classmethod
    def get_current(cls) -> "OsVersion":
        os_id: str | None = None
        os_version_id: str | None = None

        ID_PREFIX = "ID="
        VERSION_ID_PREFIX = "VERSION_ID="
        with open("/etc/os-release") as f:
            for line in f.readlines():
                if line.startswith(ID_PREFIX):
                    os_id = line.split(ID_PREFIX)[1].strip().lstrip('"').rstrip('"')
                    continue
                if line.startswith(VERSION_ID_PREFIX):
                    os_version_id = line.split(VERSION_ID_PREFIX)[1].strip().lstrip('"').rstrip('"')
                    continue
        if os_id is None or os_version_id is None:
            raise RuntimeError("Could not determine os id and version from /etc/os-release")
        return OsVersion(os_id=os_id, os_version_id=os_version_id)

    def to_docker_FROM_statement(self) -> str:
        return f"FROM {self.os_id}:{self.os_version_id}"

    def to_image_tag(self, timestamp: datetime.datetime) -> str:
        date_string = (
            f"{timestamp.year}_{timestamp.month:02}_{timestamp.day:02}__"
            f"{timestamp.hour:02}_{timestamp.minute:02}_{timestamp.second:02}"
        )
        version_str = self.os_version_id.replace('.', '_')
        os_id_str = self.os_id.replace(" ", "_").replace(".", "_")
        return f"agent_image_{os_id_str}__{version_str}__{date_string}"

    def is_fedora(self) -> bool:
        return "fedora" in self.os_id.lower()

    def package_manager(self) -> str:
        return "dnf" if self.is_fedora() else "apt"

def main():
    parser = argparse.ArgumentParser(
        description="Build an image and create a podman container from it."
    )
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--container-username", required=False, default="builder")
    parser.add_argument(
        "--ssh-port-on-host",
        required=True,
        type=int,
        help="Port that will be mapped on the host to the container's 22 port"
    )
    parser.add_argument(
        "--inject-host-pub-keys",
        action="store_true",
        default=True,
        help="Inject ~/.ssh/*.pub into the container's authorized_keys"
    )
    parser.add_argument(
        "--allow-io-uring",
        action="store_true",
        default=False,
        help="Allow io_uring syscalls inside the container. Potentially dangerous, usually unnecessary."
    )
    args = parser.parse_args()
 
    container_name: str = args.container_name
    ssh_port_on_host: int = args.ssh_port_on_host
    container_username: str = args.container_username
    container_user_home = Path("/home") / container_username
    container_ssh_dir = container_user_home / ".ssh"
    container_ssh_authorized_keys_path = container_ssh_dir / "authorized_keys"
    container_xdg_runtime_dir = container_user_home / "xdg_runtime"
    inject_host_pub_keys = args.inject_host_pub_keys
    allow_io_uring = args.allow_io_uring
 
    user_id = os.getuid()
    os_version = OsVersion.get_current()
    now = datetime.datetime.now()

    packages = [
        "systemd", # so we can boot the container, set locale, and run the ssh service
        "openssh-server", # so we can run the ssh server inside the continer
        "vulkan-tools",  "mesa-vulkan-drivers", # mesa stuff is for testing graphics
        "mesa-demos" if os_version.is_fedora() else "mesa-utils",
        "wl-clipboard", # so we can access the wayland clipboard inside the container
        "git",
        "glibc-langpack-en" if os_version.is_fedora() else "locales",
        "procps-ng" if os_version.is_fedora() else "procps",
        # "pciutils",
        # "lsb_release",
    ]

    pub_key_injections: list[str] = []
    if inject_host_pub_keys:
        for pth in Path.home().joinpath(".ssh").iterdir():
            if pth.is_file() and pth.suffix == ".pub":
                key = open(pth).read().strip()
                pub_key_injections.append(
                    f"RUN echo '{key}' >> {container_ssh_authorized_keys_path}"
                )

    subprocess.run(
        [
            "podman", "build",
            "--progress=plain",
            f"--tag={os_version.to_image_tag(now)}",
            "--file=-",
            ".",
        ],
        input=textwrap.dedent(f"""
            {os_version.to_docker_FROM_statement()}
 
            # Replace `dnf` with `apt` if you're using debian-based distros like ubuntu or mint.
            # Some of the package names might be different between distros
            RUN {os_version.package_manager()} -y update
            RUN {os_version.package_manager()} -y install {' '.join(packages)}
            RUN {os_version.package_manager()} clean all

            RUN useradd -m -s /bin/bash {container_username}
                USER {container_username}
                RUN mkdir {container_xdg_runtime_dir}
                RUN mkdir -p {container_user_home}/.ssh
                {"\n".join(pub_key_injections)}

            USER root
        """),
        text=True,
        check=True,
    )

    if allow_io_uring:
        with open("/usr/share/containers/seccomp.json", "r") as f:
            seccomp_config = json.load(f)
        seccomp_config["syscalls"].append({
            "names": ["io_uring_setup", "io_uring_enter", "io_uring_register"],
            "action": "SCMP_ACT_ALLOW"
        })
        tmp_seccomp_file = tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=True)
        tmp_seccomp_file.write(json.dumps(seccomp_config, indent=4))
        seccomp_json_args: Sequence[str] = ["--security-opt", f"seccomp={tmp_seccomp_file.name}"]
    else:
        seccomp_json_args: Sequence[str] = []
 
    # podman container create ...
    subprocess.run(
        [
            "podman", "container", "create",
            "-i",
            "-t",
            f"--name={container_name}",
            f"--hostname={container_name}",
            "-e", f"XDG_RUNTIME_DIR={container_xdg_runtime_dir}",
            "-e",  "XDG_SESSION_TYPE=wayland",
            "-e",  "WAYLAND_DISPLAY=wayland-0",
            "-e",  "COLORTERM=truecolor",
            "-e",  "TERM=xterm",
            "--security-opt", "label=disable", # this is related to selinux and might not be necessary
            *seccomp_json_args,
            "--shm-size=16g",
            "--pids-limit=-1",
            "--cap-add=CAP_SYS_NICE",
            "-v", f"/run/user/{user_id}/wayland-0:{container_xdg_runtime_dir}/wayland-0",
            "-v", "/dev/dri/card1:/dev/dri/card1",
            "-v","/dev/dri/renderD128:/dev/dri/renderD128",
            "-p", f"{ssh_port_on_host}:22",
            f"localhost/{os_version.to_image_tag(now)}",
            "/sbin/init",
            # --group-add 44 \
            # --group-add 993 \
            # --ulimit nofile=1024000:1024000 \
        ]
    )

if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        print(f"Command failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)


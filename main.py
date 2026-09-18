#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
import textwrap
import datetime


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
                    os_id = line.split(ID_PREFIX)[1].strip()
                    continue
                if line.startswith(VERSION_ID_PREFIX):
                    os_version_id = line.split(VERSION_ID_PREFIX)[1].strip()
                    continue
        if os_id is None or os_version_id is None:
            raise RuntimeError("Could not determine os id and version from /etc/os-release")
        return OsVersion(os_id=os_id, os_version_id=os_version_id)

    def to_docker_FROM_statement(self) -> str:
        return f"FROM {self.os_id}:{self.os_version_id}"

def main():
    parser = argparse.ArgumentParser(
        description="Build an image and create a podman container from it."
    )
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--container-username", required=False, default="builder")
    parser.add_argument("--ssh-port-on-host", required=True, type=int, help="Port that will be mapped on the host to the container's 22 port")
    args = parser.parse_args()
 
    container_name: str = args.container_name
    ssh_port_on_host: int = args.ssh_port_on_host
    container_username: str = args.container_username
    container_xdg_runtime_dir = f"/home/{container_username}/xdg_runtime"
 
    user_id = os.getuid()
    os_version = OsVersion.get_current()
    now = datetime.datetime.now()
    date_string = f"{now.year}_{now.month:02}_{now.day:02}__{now.hour:02}_{now.minute:02}_{now.second:02}"
    image_tag: str = f"agent_image_{os_version.os_id}__{os_version.os_version_id}__{date_string}"

    packages = [
        "systemd", # so we can boot the container, set locale, and run the ssh service
        "openssh-server", # so we can run the ssh server inside the continer
        "vulkan-tools",  "mesa-vulkan-drivers", "mesa-demos", # so we can test the GPU inside the container
        "wl-clipboard", # so we can access the wayland clipboard inside the container
        "glibc-langpack-en", # this might be related to setting the locale, but I can't remember
    ]

    print("gnna podman build.....")
    subprocess.run(
        [
            "podman", "build",
            "--progress=plain",
            f"--tag={image_tag}",
            "--file=-",
            ".",
        ],
        input=textwrap.dedent(f"""
            {os_version.to_docker_FROM_statement()}
 
            # Replace `dnf` with `apt` if you're using debian-based distros like ubuntu or mint.
            # Some of the package names might be different between distros
            RUN dnf -y update && \\
            dnf -y install {' '.join(packages)}
            RUN dnf clean all

            RUN useradd -m -s /bin/bash {container_username}
 
            USER {container_username}
            RUN mkdir {container_xdg_runtime_dir}
        """),
        text=True,
        check=True,
    )
    print("finished podman build.....")
 
    # podman container create ...
    subprocess.run(
        [
            "podman", "container", "create",
            "-i",
            "-t",
            f"--name={container_name}",
            container_name,
            f"--hostname={container_name}",
            "-e", f"XDG_RUNTIME_DIR={container_xdg_runtime_dir}",
            "-e",  "XDG_SESSION_TYPE=wayland",
            "-e",  "WAYLAND_DISPLAY=wayland-0",
            "-e",  "COLORTERM=truecolor",
            "-e",  "TERM=xterm",
            "--security-opt", "label=disable", # this is related to selinux and might not be necessary
            "--shm-size=16g",
            "--pids-limit=-1",
            "--cap-add=CAP_SYS_NICE",
            "-v", f"/run/user/{user_id}/wayland-0:{container_xdg_runtime_dir}/wayland-0",
            "-v", "/dev/dri/card1:/dev/dri/card1",
            "-v","/dev/dri/renderD128:/dev/dri/renderD128",
            "-p", f"{ssh_port_on_host}:22",
            f"localhost/{image_tag}",
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


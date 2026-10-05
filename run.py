#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py — Execution script for the Guata LCP Analysis pipeline.
Compatible with Windows, Linux and macOS.

Usage:
    python run.py

Everything printed to the terminal during a run is also written to
run_log.txt, next to this script. This is deliberate: if run.py is
started by double-clicking it instead of from a terminal, the console
window closes as soon as the run finishes (or fails) and anything that
was only on screen is lost. Writing to a file from the very first line
means there is always a record of what happened, however the script
was started.
"""

import os
import sys
import subprocess
import shutil
import platform
import datetime

DOCKER_IMAGE = "grass_lcp:v2"


# ─────────────────────────────────────────────
# Logging (terminal + file, from the first line)
# ─────────────────────────────────────────────

class Tee:
    """Writes everything to several streams at once (terminal and log file)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def setup_run_log():
    """
    Redirects stdout and stderr so everything printed from here on is
    also saved to run_log.txt, next to run.py. Returns the log file
    handle so it can be closed cleanly at the end of the run.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "run_log.txt")

    log_file = open(log_path, "a", encoding="utf-8")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(f"\n{'=' * 60}\nRun started: {timestamp}\n{'=' * 60}\n")
    log_file.flush()

    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)

    return log_file, log_path


# ─────────────────────────────────────────────
# Terminal utilities
# ─────────────────────────────────────────────

def print_header():
    print()
    print("=" * 60)
    print("  Least Cost Path (LCP) Pipeline")
    print("=" * 60)
    print()

def print_step(n, text):
    print(f"\n[{n}] {text}")
    print("-" * 40)

def print_ok(msg):
    print(f"  OK: {msg}")

def print_error(msg):
    print(f"  ERROR: {msg}")

def print_warning(msg):
    print(f"  ! {msg}")


# ─────────────────────────────────────────────
# Docker and image check
# ─────────────────────────────────────────────

def check_docker():
    print_step(1, "Checking Docker")

    if shutil.which("docker") is None:
        print_error("Docker was not found on this system.")
        print()
        print("  Install Docker Desktop at: https://www.docker.com/products/docker-desktop")
        print("  After installing, restart the terminal and run this script again.")
        sys.exit(1)

    print_ok("Docker found.")

    result = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if result.returncode != 0:
        print_error("Docker is installed, but it is not running.")
        print()
        print("  Open Docker Desktop and wait for it to start,")
        print("  then run this script again.")
        sys.exit(1)

    print_ok("Docker is running.")

    # Checks whether the grass_lcp:v2 image exists locally
    result = subprocess.run(
        ["docker", "image", "inspect", DOCKER_IMAGE],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print_error(f"Image '{DOCKER_IMAGE}' was not found.")
        print()
        print("  Make sure the image was loaded correctly into Docker.")
        print("  You can check available images with: docker images")
        sys.exit(1)

    print_ok(f"Image '{DOCKER_IMAGE}' found.")


# ─────────────────────────────────────────────
# Folder collection and validation
# ─────────────────────────────────────────────

def ask_for_folder(description, must_exist=True):
    while True:
        print(f"  {description}")
        path = input("  Path: ").strip()
        path = path.strip('"').strip("'")

        if not path:
            print_warning("Path cannot be empty. Try again.")
            continue

        path = os.path.abspath(path)

        if must_exist and not os.path.isdir(path):
            print_warning(f"Folder not found: {path}")
            print_warning("Check the path and try again.")
            continue

        if not must_exist:
            os.makedirs(path, exist_ok=True)
            print_ok(f"Folder created/verified: {path}")

        return path


def validate_data_folder(data_folder):
    """
    Checks whether the data folder contains everything the pipeline needs.
    Returns (problems, n_tifs, n_vectors), where 'problems' is a list of
    human-readable issues (empty list means the folder is OK).
    """
    problems = []

    tifs = [f for f in os.listdir(data_folder) if f.endswith('.tif')]
    vectors = [f for f in os.listdir(data_folder) if f.endswith('.shp') or f.endswith('.gpkg')]
    points_folder = os.path.join(data_folder, "divided_points")

    if not tifs:
        problems.append("No .tif file found in the data folder.")
    if not vectors:
        problems.append("No .shp or .gpkg file found in the data folder.")
    if not os.path.isdir(points_folder):
        problems.append("'divided_points' subfolder not found inside the data folder.")
    elif not any(f.endswith(".gpkg") for f in os.listdir(points_folder)):
        problems.append("'divided_points' subfolder exists but contains no .gpkg files.")

    return problems, len(tifs), len(vectors)


def collect_folders():
    print_step(2, "Folder configuration")

    script_dir = os.path.dirname(os.path.abspath(__file__))

    default_data_folder = os.path.join(script_dir, "data")
    default_grass_folder = os.path.join(script_dir, "grassdata")
    default_output_folder = os.path.join(script_dir, "output")

    print()
    print(f"  Current folder: {script_dir}")

    if os.path.isdir(default_data_folder):
        print_ok(f"'data' folder found automatically: {default_data_folder}")
        data_folder = default_data_folder
    else:
        print()
        print_warning(f"'data' folder not found in: {script_dir}")
        print("  DATA folder (must contain the .tif, the .shp or .gpkg")
        print("  and the 'divided_points' subfolder with the points):")
        data_folder = ask_for_folder("Enter the full path:", must_exist=True)

    # Validate the data folder. If something required is missing (.tif,
    # .shp/.gpkg or the divided_points subfolder), the pipeline inside the
    # container will always fail later with the exact same check — so
    # instead of offering to "continue anyway", we ask for a corrected
    # path right away and keep validating until everything is in place.
    while True:
        problems, n_tifs, n_vectors = validate_data_folder(data_folder)

        if not problems:
            print_ok(f"Data found: {n_tifs} raster(s), {n_vectors} vector(s).")
            break

        print()
        for p in problems:
            print_warning(p)
        print()
        print("  The pipeline cannot run without these files.")
        print("  Please provide the correct data folder (the one that")
        print("  contains the .tif, the .shp/.gpkg and 'divided_points'):")
        data_folder = ask_for_folder("Path to the data folder:", must_exist=True)

    print()
    if os.path.isdir(default_grass_folder):
        print_ok(f"'grassdata' folder found automatically: {default_grass_folder}")
        grass_folder = default_grass_folder
    else:
        os.makedirs(default_grass_folder, exist_ok=True)
        print_ok(f"'grassdata' folder created automatically: {default_grass_folder}")
        grass_folder = default_grass_folder

    print()
    if os.path.isdir(default_output_folder):
        print_ok(f"'output' folder found automatically: {default_output_folder}")
        output_folder = default_output_folder
    else:
        os.makedirs(default_output_folder, exist_ok=True)
        print_ok(f"'output' folder created automatically: {default_output_folder}")
        output_folder = default_output_folder

    return data_folder, grass_folder, output_folder


# ─────────────────────────────────────────────
# Cost function selection
# ─────────────────────────────────────────────

def ask_cost_function():
    """
    Asks for the cost function here, in run.py, instead of letting the
    container prompt for it. This keeps the docker run call fully
    non-interactive, which matters for two reasons: double-clicking
    run.py often has no real terminal attached for Docker to prompt
    into, and a non-interactive call is what lets us capture and log
    the pipeline's full output (see run_pipeline below).
    """
    print_step(3, "Cost function selection")
    print()
    while True:
        choice = input("  Choose cost function - Tobler (T), Herzog (H) or Langmuir (L): ").strip().upper()
        if choice in ["T", "TOBLER"]:
            return "tobler"
        elif choice in ["H", "HERZOG"]:
            return "herzog"
        elif choice in ["L", "LANGMUIR"]:
            return "langmuir"
        else:
            print_warning("Invalid choice. Enter 'T', 'H' or 'L'.")


# ─────────────────────────────────────────────
# Container execution
# ─────────────────────────────────────────────

def to_docker_path(path):
    """Converts Windows paths to Docker format."""
    if platform.system() == "Windows":
        path = path.replace("\\", "/")
        if len(path) >= 2 and path[1] == ":":
            drive_letter = path[0].lower()
            path = f"/{drive_letter}{path[2:]}"
    return path


def run_pipeline(data_folder, grass_folder, output_folder, cost_function):
    print_step(4, "Running analysis")
    print()
    print_warning("Processing may take several minutes depending on data size.")
    print()

    # Detects the path of lcp_pipeline.py (same folder as this run.py)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_script = os.path.join(script_dir, "lcp_pipeline.py")

    if not os.path.isfile(local_script):
        print_error("lcp_pipeline.py not found in the same folder as run.py.")
        sys.exit(1)

    d_data   = to_docker_path(data_folder)
    d_grass  = to_docker_path(grass_folder)
    d_output = to_docker_path(output_folder)
    d_script = to_docker_path(local_script)

    # No "-it": the cost function is already decided above, so the
    # container never needs to read from stdin, and running without a
    # TTY lets us capture its output line by line below instead of
    # just letting it print straight to whatever console is attached
    # (or isn't, when run.py was started by double-clicking it).
    command = [
        "docker", "run", "--rm",
        "-e", "HOME=/tmp",
        "-v", f"{d_data}:/app/data",
        "-v", f"{d_grass}:/app/grassdata",
        "-v", f"{d_output}:/app/output",
        "-v", f"{d_script}:/app/lcp_pipeline.py",
        DOCKER_IMAGE,
        "python", "/app/lcp_pipeline.py",
        "/app/grassdata",
        "/app/output",
        "--cost-function", cost_function,
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout:
        print(line, end="")
    returncode = process.wait()

    print()
    if returncode == 0:
        print("=" * 60)
        print_ok("Analysis completed successfully!")
        print(f"  Results were saved to:\n  {output_folder}")
        print("=" * 60)
    else:
        print("=" * 60)
        print_error("The pipeline finished with errors.")
        print("  Check the messages above for more details.")
        print("=" * 60)
        sys.exit(1)


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────

def main():
    print_header()
    check_docker()
    data_folder, grass_folder, output_folder = collect_folders()
    cost_function = ask_cost_function()
    run_pipeline(data_folder, grass_folder, output_folder, cost_function)


if __name__ == "__main__":
    log_file, log_path = setup_run_log()
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Execution cancelled by user.")
        sys.exit(0)
    finally:
        print(f"\n  Full log saved to: {log_path}")
        log_file.close()

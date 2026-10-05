#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Guata LCP Analysis pipeline via GRASS GIS.

Calculates accumulated cost surfaces (Tobler, Herzog or Langmuir) from a
DEM, generates least cost paths between an origin point grid and a
destination grid, and produces a path density raster.

Usage:
    python lcp_pipeline.py <grassdata_folder> <output_folder> [--cost-function {tobler,herzog,langmuir}] [--workers N] [--resolution N]

Expected structure in /app/data:
    /app/data/*.tif                  -> DEM (raster)
    /app/data/*.shp or *.gpkg        -> destination point grid
    /app/data/divided_points/*.gpkg  -> individual origin points
"""

import os
import sys
import glob
import shutil
import time
import logging
import argparse
import multiprocessing as mp
from functools import partial

import geopandas as gpd
import rasterio
import fiona
import numpy as np
from rasterio.features import rasterize

import grass.script as gscript
from grass_session import Session


# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────

def setup_logging(output_path, log_suffix):
    """
    Configures logging to console and file. Log file name includes the
    cost function suffix and run number (e.g. pipeline_tobler.log,
    pipeline_herzog_1.log), matching the other output files so each
    run keeps its own log and nothing is ever overwritten.
    """
    log_file = os.path.join(output_path, f"pipeline_{log_suffix}.log")
    os.makedirs(output_path, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, mode="w", encoding="utf-8"),
        ],
    )
    return logging.getLogger("lcp_pipeline")


class StepTimer:
    """Context manager that measures and logs the duration of each pipeline step."""

    def __init__(self, logger, label):
        self.logger = logger
        self.label = label
        self.start = None

    def __enter__(self):
        self.start = time.time()
        self.logger.info(f"START: {self.label}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.start
        if exc_type is None:
            self.logger.info(f"DONE: {self.label} ({elapsed:.2f}s)")
        else:
            self.logger.error(f"FAILED: {self.label} after {elapsed:.2f}s -> {exc_val}")
        return False  # do not suppress exceptions


# ─────────────────────────────────────────────
# Folder / path utilities
# ─────────────────────────────────────────────

def clean_folder(path, folder_name, logger):
    """Ensures the folder exists and is empty before a new run."""
    if not os.path.isdir(path):
        logger.info(f"Folder '{folder_name}' does not exist. Creating: {path}")
        os.makedirs(path, exist_ok=True)
        return

    contents = os.listdir(path)
    if contents:
        logger.info(f"Folder '{folder_name}' is not empty. Cleaning contents...")
        for item in contents:
            item_path = os.path.join(path, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                logger.warning(f"Could not remove {item_path}: {e}")
        logger.info(f"Folder '{folder_name}' cleaned successfully.")
    else:
        logger.info(f"Folder '{folder_name}' is already empty.")


def detect_input_files(base_path, logger):
    """Automatically detects the DEM (.tif) and the destination grid (.shp/.gpkg)."""
    tif_files = glob.glob(os.path.join(base_path, "*.tif"))
    raster_input = tif_files[0] if tif_files else None

    vector_files = (
        glob.glob(os.path.join(base_path, "*.shp"))
        + glob.glob(os.path.join(base_path, "*.gpkg"))
    )
    vector_input = vector_files[0] if vector_files else None

    points_folder = os.path.join(base_path, "divided_points")

    if not raster_input or not vector_input:
        logger.error(
            "Make sure there is a .tif and a .shp/.gpkg file in "
            f"{base_path}"
        )
        sys.exit(1)

    if not os.path.isdir(points_folder):
        logger.error(f"'divided_points' subfolder not found in {base_path}")
        sys.exit(1)

    logger.info(f"Raster detected: {raster_input}")
    logger.info(f"Destination vector detected: {vector_input}")
    return raster_input, vector_input, points_folder


# ─────────────────────────────────────────────
# Cost function selection
# ─────────────────────────────────────────────

COST_FUNCTIONS = {
    "tobler": "Tobler (walking speed)",
    "herzog": "Herzog (metabolic cost)",
    "langmuir": "Langmuir (elevation-based time cost)",
}


def select_cost_function(preselected=None):
    """Prompts the user for the cost function, or uses the preselected one."""
    if preselected:
        key = preselected.strip().lower()
        if key in COST_FUNCTIONS:
            return key
        print(f"Invalid cost function: '{preselected}'. Use tobler, herzog or langmuir.")
        sys.exit(1)

    print("=" * 50)
    print("COST FUNCTION SELECTION")
    print("=" * 50)
    while True:
        choice = input("Choose cost function - Tobler (T), Herzog (H) or Langmuir (L): ").strip().upper()
        if choice in ["T", "TOBLER"]:
            return "tobler"
        elif choice in ["H", "HERZOG"]:
            return "herzog"
        elif choice in ["L", "LANGMUIR"]:
            return "langmuir"
        else:
            print("Invalid choice. Enter 'T', 'H' or 'L'.")


# ─────────────────────────────────────────────
# Modular GRASS pipeline steps
# ─────────────────────────────────────────────

def import_dem(raster_input, logger):
    """Imports the DEM into the GRASS session and sets the working region."""
    gscript.run_command("g.region", flags="d")
    gscript.run_command("r.import", flags="o", input=raster_input, output="dem")
    gscript.run_command("g.region", raster="dem")
    logger.info("DEM imported and working region set.")


def calculate_slope(dem_input="dem", slope_output="slope_percent_raster",
                     slope_format="percent", logger=None):
    """
    Calculates terrain slope from the DEM.

    slope_format: output format of r.slope.aspect. 'percent' is the
    default used by the Tobler and Herzog formulas; it can be changed to
    'degrees' if a cost formula requires slope in radians or degrees
    instead of percent.
    """
    gscript.run_command(
        "r.slope.aspect",
        elevation=dem_input,
        slope=slope_output,
        format=slope_format,
        overwrite=True,
    )

    if not gscript.find_file(name=slope_output, element="cell")["file"]:
        raise RuntimeError(f"Slope raster '{slope_output}' was not created.")

    if logger:
        logger.info(f"Slope calculated ({slope_format}): {slope_output}")
    return slope_output


def calculate_cost_surface(cost_function, slope_map, final_output="final_output_raster"):
    """
    Applies the chosen cost formula on top of the slope map.

    Each formula lives in its own sub-function to make maintenance,
    isolated testing, and adding new cost functions easier in the future.
    """
    slope_decimal = f"({slope_map} / 100.0)"

    if cost_function == "tobler":
        return _cost_tobler(slope_decimal, final_output)
    elif cost_function == "herzog":
        return _cost_herzog(slope_decimal, final_output)
    else:
        raise ValueError(f"Unknown cost function: {cost_function}")


def _cost_tobler(slope_decimal, final_output):
    """Tobler's hiking function (1993): walking speed -> cost in m/s."""
    gscript.run_command(
        "r.mapcalc",
        expression=f"tobler_km_h = 6.0 * exp(-3.5 * abs({slope_decimal} + 0.05))",
        overwrite=True,
    )
    gscript.run_command(
        "r.mapcalc",
        expression=f"{final_output} = 1.0 / ((6.0 * exp(-3.5 * abs({slope_decimal} + 0.05))) / 3.6)",
        overwrite=True,
    )
    return final_output


def _cost_herzog(slope_decimal, final_output):
    """Herzog-Minetti function: metabolic cost polynomial (W/kg)."""
    expression = (
        f"{final_output} = 1337.8 * pow({slope_decimal}, 6) + "
        f"278.19 * pow({slope_decimal}, 5) - "
        f"517.39 * pow({slope_decimal}, 4) - "
        f"78.199 * pow({slope_decimal}, 3) + "
        f"93.419 * pow({slope_decimal}, 2) + "
        f"19.825 * {slope_decimal} + 1.64"
    )
    gscript.run_command("r.mapcalc", expression=expression, overwrite=True)
    return final_output


def prepare_friction_surface(cost_function, logger):
    """
    Prepares the friction surface according to the chosen cost function.

    Langmuir does not depend on a pre-calculated slope: it uses a neutral
    friction map and lets r.walk model the cost from raw elevation, via
    the formula's own coefficients.
    """
    if cost_function == "langmuir":
        logger.info("Langmuir selected: creating neutral friction map (no slope calculation needed).")
        gscript.run_command("r.mapcalc", expression="neutral_friction = 1.0", overwrite=True)
        return "neutral_friction"

    slope_map = calculate_slope(logger=logger)
    final_output = calculate_cost_surface(cost_function, slope_map)

    if not gscript.find_file(name=final_output, element="cell")["file"]:
        raise RuntimeError(f"Cost surface '{final_output}' was not created.")

    logger.info(f"Cost surface '{cost_function}' calculated: {final_output}")
    return final_output


def import_vector_grid(vector_input, points_folder, logger):
    """Imports the destination grid and all the individual origin points."""
    gscript.run_command("v.import", input=vector_input, output="points", overwrite=True)

    vector_files = [f for f in os.listdir(points_folder) if f.endswith(".gpkg")]
    if not vector_files:
        raise RuntimeError(f"No .gpkg files found in {points_folder}")

    for vector_file in vector_files:
        input_path = os.path.join(points_folder, vector_file)
        output_name = os.path.splitext(vector_file)[0]
        gscript.run_command("v.import", input=input_path, output=output_name, overwrite=True)

    logger.info(f"{len(vector_files)} origin point files imported.")
    return "points"


# ─────────────────────────────────────────────
# Sequential r.walk
# ─────────────────────────────────────────────

WALK_COEFFICIENTS = {
    "tobler": {"walk_coeff": "1,0,0,0"},
    "herzog": {"walk_coeff": "1,0,0,0", "slope_factor": "0.16"},
    "langmuir": {"walk_coeff": "0.72,6.0,1.99,-1.99"},
}


def _run_single_walk(source_layer, cost_function, friction_map,
                      destination_points, output_prefix, direction_prefix,
                      gisdb, location, mapset):
    """
    Runs a single r.walk call inside the already-open GRASS session.

    NOTE: No sub-session is opened here. The function relies on the
    environment variables set by the main session (in main()). Opening
    and closing a GSession inside each worker was causing the GRASS
    environment to be torn down after the last worker finished, which
    made g.list (and all subsequent GRASS commands) unavailable for
    r.drain and the rest of the pipeline.
    """
    import os as _os
    import time as _time
    import grass.script as gs

    pid = _os.getpid()
    output = f"{output_prefix}_{source_layer}"
    outdir = f"{direction_prefix}_{source_layer}"
    t_start = _time.time()

    try:
        kwargs = dict(
            elevation="dem",
            friction=friction_map,
            start_points=source_layer,
            stop_points=destination_points,
            output=output,
            outdir=outdir,
            flags="k",
            overwrite=True,
        )
        kwargs.update(WALK_COEFFICIENTS[cost_function])

        gs.run_command("r.walk", **kwargs)
        elapsed = _time.time() - t_start
        return (source_layer, True, None, pid, elapsed)
    except Exception as e:
        elapsed = _time.time() - t_start
        return (source_layer, False, str(e), pid, elapsed)


def run_walk_parallel(cost_function, friction_map, source_points_prefix,
                       destination_points, output_prefix, direction_prefix,
                       gisdb, location, mapset, logger, max_workers=None):
    """
    Runs r.walk for all origin points sequentially (max_workers=1) or
    in parallel (max_workers>1).

    When max_workers=1 the loop runs directly in the main process,
    keeping the GRASS session alive for all subsequent pipeline steps.
    Parallel mode (max_workers>1) is kept for future use but requires
    careful handling of GRASS session state across processes.
    """
    source_layers = gscript.list_strings(type="vector", pattern=f"{source_points_prefix}*")

    if not source_layers:
        logger.warning("No origin points found for r.walk.")
        return []

    total_cpus = mp.cpu_count()
    if max_workers is None:
        max_workers = max(1, min(len(source_layers), total_cpus - 1 or 1))

    # ── Parallelization diagnostic header ──────────────────────────
    logger.info("=" * 50)
    logger.info("PARALLELIZATION DIAGNOSTICS")
    logger.info(f"  CPUs available in container : {total_cpus}")
    logger.info(f"  Workers requested           : {max_workers}")
    logger.info(f"  Origin points to process    : {len(source_layers)}")
    logger.info(f"  Mode                        : {'parallel' if max_workers > 1 else 'sequential (main session)'}")
    logger.info("=" * 50)

    worker_fn = partial(
        _run_single_walk,
        cost_function=cost_function,
        friction_map=friction_map,
        destination_points=destination_points,
        output_prefix=output_prefix,
        direction_prefix=direction_prefix,
        gisdb=gisdb,
        location=location,
        mapset=mapset,
    )

    results = []
    wall_start = time.time()

    # Sequential: runs in the main process, preserving the GRASS session.
    if max_workers <= 1:
        for layer in source_layers:
            results.append(worker_fn(layer))
    else:
        with mp.Pool(processes=max_workers) as pool:
            results = pool.map(worker_fn, source_layers)

    wall_elapsed = time.time() - wall_start

    # ── Per-point log ───────────────────────────────────────────────
    unique_pids = set()
    ok_count = 0
    total_cpu_time = 0.0

    for layer, success, error, pid, elapsed in results:
        unique_pids.add(pid)
        total_cpu_time += max(elapsed, 0.0)  # guard against clock skew
        if success:
            ok_count += 1
            logger.info(f"  OK  {layer:<30} PID={pid}  {elapsed:.2f}s")
        else:
            logger.warning(f"  FAIL {layer:<30} PID={pid}  {elapsed:.2f}s  -> {error}")

    # ── Parallelization summary ─────────────────────────────────────
    logger.info("=" * 50)
    logger.info("PARALLELIZATION SUMMARY")
    logger.info(f"  Points succeeded  : {ok_count}/{len(source_layers)}")
    logger.info(f"  Unique PIDs used  : {len(unique_pids)} {sorted(unique_pids)}")
    logger.info(f"  Wall-clock time   : {wall_elapsed:.2f}s")
    logger.info(f"  Total CPU time    : {total_cpu_time:.2f}s")
    if wall_elapsed > 0:
        effective = total_cpu_time / wall_elapsed
        logger.info(f"  Effective speedup : {effective:.2f}x  "
                    f"({'parallelization working' if effective > 1.3 else 'sequential — single session mode'})")
    logger.info("=" * 50)

    return [layer for layer, success, _, _pid, _elapsed in results if success]


# ─────────────────────────────────────────────
# r.drain and path network merge
# ─────────────────────────────────────────────

def run_drain(output_prefix, direction_prefix, source_points_prefix,
              destination_points, logger):
    """Traces least cost paths (LCP) from the r.walk cost surfaces."""
    cost_maps = gscript.list_strings(type="raster", pattern=f"{output_prefix}_{source_points_prefix}*")
    generated = []

    for cost_map in cost_maps:
        direction_map = cost_map.replace(output_prefix, direction_prefix)
        base_name = cost_map.replace(f"{output_prefix}_", "")
        lcp_output = f"lcp_{base_name}"
        drain_output = f"drain_{base_name}"

        try:
            gscript.run_command(
                "r.drain",
                input=cost_map,
                direction=direction_map,
                output=lcp_output,
                drain=drain_output,
                start_points=destination_points,
                overwrite=True,
                flags="dc",
            )
            if gscript.find_file(name=drain_output, element="vector")["name"]:
                generated.append(drain_output)
            else:
                logger.warning(f"{drain_output} ignored by GRASS (geometrically impossible path).")
        except Exception as e:
            logger.error(f"Error processing {base_name}: {e}")

    logger.info(f"r.drain done: {len(generated)} paths generated.")
    return generated


def merge_paths(drain_outputs, merged_vector_name, logger):
    """Merges all individual paths (drain_*) into a single path network."""
    if not drain_outputs:
        raise RuntimeError("No 'drain_*' map found. r.drain failed for all IDs.")

    gscript.run_command(
        "v.patch",
        input=",".join(drain_outputs),
        output=merged_vector_name,
        overwrite=True,
    )
    logger.info(f"Merged vector '{merged_vector_name}' created with {len(drain_outputs)} paths.")
    return merged_vector_name


# ─────────────────────────────────────────────
# Path density (batch rasterization)
# ─────────────────────────────────────────────

def next_run_suffix(output_path, cost_function):
    """
    Determines the suffix for this run's output files, so that
    re-running the same cost function does not overwrite previous
    results. Returns the cost_function name for the first run, or
    '<cost_function>_1', '<cost_function>_2', etc. for subsequent runs.
    """
    index = 0
    while True:
        tag = cost_function if index == 0 else f"{cost_function}_{index}"
        candidate = os.path.join(output_path, f"lcp_net_{tag}.gpkg")
        if not os.path.exists(candidate):
            return tag
        index += 1


def export_path_network(merged_vector, output_path, suffix, logger):
    """Exports the merged path network to GeoPackage."""
    net_path = os.path.join(output_path, f"lcp_net_{suffix}.gpkg")
    gscript.run_command("v.out.ogr", input=merged_vector, output=net_path, format="GPKG", overwrite=True)
    logger.info(f"Path network exported: {net_path}")
    return net_path


def rasterize_path_density(net_path, output_path, suffix, resolution=100,
                            batch_size=10000, logger=None):
    """
    Generates the path density raster from the vector network.

    Reads the vector file in batches via fiona, avoiding loading the
    whole network into memory at once (relevant for networks with more
    than a million paths). The merge_alg=add parameter sums overlaps,
    which can produce count noise at very close crossings; if this
    becomes a problem, consider applying a smoothing filter (e.g.
    median) on the final raster before analysis.
    """
    output_file = os.path.join(output_path, f"lcp_dens_{suffix}.tif")

    with fiona.open(net_path) as source:
        bounds = source.bounds
        crs = source.crs
        total_paths = len(source)
        if logger:
            logger.info(f"Bounds: {bounds} | Total paths: {total_paths}")

    width = int((bounds[2] - bounds[0]) / resolution)
    height = int((bounds[3] - bounds[1]) / resolution)
    transform = rasterio.transform.from_bounds(*bounds, width, height)

    if logger:
        logger.info(f"Creating density matrix: {width}x{height} pixels (resolution {resolution}m).")

    try:
        path_counts = np.zeros((height, width), dtype=np.uint32)
    except MemoryError:
        raise RuntimeError("Not enough memory to create the matrix. Increase the 'resolution' value.")

    with fiona.open(net_path) as source:
        batch = []
        for i, feature in enumerate(source):
            if feature["geometry"] is not None:
                batch.append((feature["geometry"], 1))

            if len(batch) >= batch_size:
                rasterize(batch, out=path_counts, transform=transform,
                          all_touched=True, merge_alg=rasterio.enums.MergeAlg.add)
                batch = []
                if logger and (i + 1) % (batch_size * 2) == 0:
                    logger.info(f"Progress: {i + 1} paths processed...")

        if batch:
            rasterize(batch, out=path_counts, transform=transform,
                      all_touched=True, merge_alg=rasterio.enums.MergeAlg.add)

    with rasterio.open(
        output_file, "w",
        driver="GTiff",
        height=height, width=width,
        count=1, dtype="uint32",
        crs=crs, transform=transform,
        compress="lzw", nodata=0, tiled=True,
    ) as dst:
        dst.write(path_counts, 1)

    if logger:
        logger.info(f"Density raster saved: {output_file}")
    return output_file


# ─────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Least Cost Path (LCP) pipeline via GRASS GIS.")
    parser.add_argument("grassdata", nargs="?", default="/app/grassdata", help="GRASSDATA folder")
    parser.add_argument("output", nargs="?", default="/app/output", help="Output folder")
    parser.add_argument("--cost-function", choices=list(COST_FUNCTIONS.keys()), default=None,
                         help="Cost function (skips interactive selection)")
    parser.add_argument("--workers", type=int, default=1,
                         help="Number of parallel processes for r.walk (default: 1 = sequential)")
    parser.add_argument("--resolution", type=int, default=100,
                         help="Density raster resolution, in meters (default: 100)")
    return parser.parse_args()


def main():
    args = parse_args()

    gisdbase_path = args.grassdata
    output_path = args.output

    # Select cost function and output suffix BEFORE setup_logging,
    # so the log file gets the same name as the other output files.
    os.makedirs(output_path, exist_ok=True)
    cost_function = select_cost_function(preselected=args.cost_function)
    output_tag = next_run_suffix(output_path, cost_function)

    logger = setup_logging(output_path, output_tag)
    pipeline_start = time.time()

    logger.info("=" * 50)
    logger.info("FOLDER CONFIGURATION")
    logger.info(f"Grassdata: {gisdbase_path}")
    logger.info(f"Output:    {output_path}")
    logger.info("=" * 50)

    clean_folder(gisdbase_path, "grassdata", logger)
    # Output folder is intentionally NOT cleaned between runs.
    # Results from previous runs are preserved and new ones get a
    # unique suffix (see next_run_suffix), so nothing is ever lost.

    base_path = "/app/data"
    raster_input, vector_input, points_folder = detect_input_files(base_path, logger)

    location_name = "docker_location_31984"
    mapset_name = "PERMANENT"
    projection_epsg = 31984
    location_path = os.path.join(gisdbase_path, location_name)
    create_arg = f"EPSG:{projection_epsg}" if not os.path.isdir(location_path) else None

    if not os.path.isdir(gisdbase_path):
        os.makedirs(gisdbase_path, exist_ok=True)

    logger.info(f"Selected cost function: {COST_FUNCTIONS[cost_function]}")

    session = Session()
    try:
        with StepTimer(logger, "Opening GRASS session"):
            session.open(gisdb=gisdbase_path, location=location_name,
                         mapset=mapset_name, create_opts=create_arg)

        with StepTimer(logger, "Importing DEM"):
            import_dem(raster_input, logger)

        with StepTimer(logger, f"Calculating cost surface ({cost_function})"):
            friction_map = prepare_friction_surface(cost_function, logger)

        with StepTimer(logger, "Importing point grid"):
            destination_points = import_vector_grid(vector_input, points_folder, logger)

        with StepTimer(logger, "r.walk (sequential)"):
            run_walk_parallel(
                cost_function=cost_function,
                friction_map=friction_map,
                source_points_prefix="id_",
                destination_points=destination_points,
                output_prefix="cost_output",
                direction_prefix="direction_output",
                gisdb=gisdbase_path,
                location=location_name,
                mapset=mapset_name,
                logger=logger,
                max_workers=args.workers,
            )

        with StepTimer(logger, "r.drain"):
            drain_outputs = run_drain(
                output_prefix="cost_output",
                direction_prefix="direction_output",
                source_points_prefix="id_",
                destination_points=destination_points,
                logger=logger,
            )

        with StepTimer(logger, "Merging path network (v.patch)"):
            merged_vector = merge_paths(drain_outputs, "merged_points", logger)

        with StepTimer(logger, "Exporting path network"):
            net_path = export_path_network(merged_vector, output_path, output_tag, logger)

        with StepTimer(logger, "Rasterizing path density"):
            rasterize_path_density(
                net_path, output_path, output_tag,
                resolution=args.resolution, logger=logger,
            )

        total_elapsed = time.time() - pipeline_start
        logger.info("=" * 50)
        logger.info(f"PIPELINE COMPLETED IN {total_elapsed:.2f}s ({total_elapsed / 60:.1f} min)")
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"Fatal pipeline error: {e}")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
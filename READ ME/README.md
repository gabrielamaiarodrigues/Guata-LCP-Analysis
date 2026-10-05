# Guata LCP Analysis

'Guata Least Cost Path Analysis', whose name comes from the Indigenous Brazilian Tupi language where Guata (/gwa.'ta/) means 'to walk', is a script for archaeological spatial analysis built on GRASS GIS. It takes a DEM and a set of origin and destination points, computes an accumulated cost surface using one of three cost functions (Tobler, Herzog or Langmuir), traces the least cost paths between origins and destinations, and outputs a path density raster showing where movement across the landscape tends to concentrate.

## How it works, and what changed from the original script

Guata grew out of an older, single-file version of this pipeline. The code has since been split into modular steps, DEM import, cost surface calculation, path tracing, merging and rasterizing, each timed and logged on its own, so it's easy to see where a slow run is actually spending its time. Path tracing moved from GRASS's `r.cost` to `r.walk`, which is what makes it possible to choose between the Tobler, Herzog and Langmuir cost functions instead of being limited to a single cost model. For runs with many origin points, `r.walk` can also be split across multiple parallel processes instead of handling one point at a time, which cuts runtime roughly in proportion to the number of cores available, though this mode is still optional rather than the default (more on that below).

## Setting up Docker

Guata runs inside a Docker container, so GRASS and all its dependencies come ready to use and you don't need to install them yourself. Download Docker Desktop (Windows or Mac) or Docker Engine (Linux) from docker.com and run the installer for your system. Once it's installed, open Docker Desktop and make sure it's actually running in the background (its icon in the system tray or menu bar should show it as active) before using any of the commands below.

If you plan to use `--workers` for parallel runs, go to Docker Desktop's Settings > Resources and check how many CPUs are allocated to the engine, raising it if needed, since by default it can be set lower than what your machine actually has available. Allocating more RAM there also helps when working with a large DEM.

## Project folder and running it

Guata works best when the data folder, the output folder and the project files (`run.py`, `lcp_pipeline.py`, the Dockerfile) all sit together inside the same folder on your computer, usually called `main_files`. When everything is in one place, `run.py` finds it all automatically. If a folder or file is somewhere else, `run.py` will ask you to type in its path instead.

To grab a path quickly: on Windows, hold Shift and right-click the folder or file, then choose "Copy as path"; on Mac, right-click while holding Option and choose "Copy as Pathname", or simply drag the folder into the terminal window and it will paste the path in for you.

To run the pipeline, open a terminal from inside the `main_files` folder (right-click inside the folder and choose "Open in Terminal", or `cd` into it manually) and run:

```
python run.py
```

`run.py` checks that Docker is installed and running, locates (or asks for) your data, grassdata and output folders, then asks you to choose the cost function, Tobler, Herzog or Langmuir, directly in the terminal before starting the analysis. You can also just double-click `run.py` instead of using the terminal, it works the same way.

Every run writes a full log to `run_log.txt`, inside the `main_files` folder, covering everything from the Docker checks to the complete pipeline run, regardless of whether you started `run.py` from the terminal or by double-clicking it, so there's always a record to check back on if something fails.

If you already have the exported image, load it directly:

```
docker load -i grass_lcp.tar
```

Otherwise build it from the Dockerfile in this repository:

```
docker build -t grass_lcp:v2 .
```

Once the image is ready, run the pipeline against your data folder:

```
docker run -v /path/to/your/data:/app/data -v /path/to/output:/app/output grass_lcp:v2 python lcp_pipeline.py
```

`grassdata` and `output` are positional and optional, defaulting to `/app/grassdata` and `/app/output` inside the container. The pipeline asks you to choose a cost function interactively unless you pass one directly:

```
--cost-function {tobler,herzog,langmuir}   skip the interactive prompt
--workers N                                 number of parallel processes for r.walk (default: 1, sequential)
--resolution N                              resolution of the output density raster, in meters (default: 100)
```

`--workers` above 1 runs r.walk in parallel, but it currently needs careful handling of GRASS session state across processes and isn't the default yet.

## Input data

The pipeline expects the following inside `/app/data`:

```
/app/data/*.tif                  DEM (raster)
/app/data/*.shp or *.gpkg        destination point grid
/app/data/divided_points/*.gpkg  individual origin points
```

## Output

Guata writes three things to your output folder. `pipeline.log` is the full run log, with every step timed separately, from DEM import to the final raster, so you can see where time went on a given run. `lcp_net_<cost_function>.gpkg` is a GeoPackage with the complete least cost path network, one line per origin-destination pair. `lcp_dens_<cost_function>.tif` is the path density raster at the resolution you chose, showing where paths overlap the most, usually the main result for interpreting movement patterns across the landscape.

## License

GPL-3.0. This pipeline depends directly on the GRASS GIS Python API (itself GPL-licensed), so the code here follows the same license.

## Citation

If you use this pipeline, please cite it using the metadata in `CITATION.cff`.

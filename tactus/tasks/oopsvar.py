"""OopsVar task — 3D-Var with OOPS (screening and minimisation in one OOVAR run)."""

import json
import os
import shutil
from datetime import timedelta

import yaml

from ..config_parser import ConfigPaths
from ..datetime_utils import as_datetime, split_date, td2min
from ..logs import logger
from ..namelist import NamelistGenerator
from ..obs_utils import Observations, odb_io_env
from ..os_utils import tactusmakedirs
from .base import Task
from .batch import BatchJob

# OOVAR namelist targets and the files they are written to
OOVAR_NAMELISTS = {
    "oovar_fort4": "fort.4",
    "oovar_obs": "naml_observations",
    "oovar_obs_tlad": "naml_observations_tlad",
    "oovar_bmatrix": "naml_bmatrix",
    "oovar_geometry": "naml_standard_geometry",
    "oovar_traj": "naml_traj_model",
    "oovar_linmod": "naml_linear_model",
    "oovar_nonlinmod": "naml_nonlinear_model",
    "oovar_write_spec": "naml_oops_write_spec",
    "oovar_write_analysis": "naml_write_analysis",
    "oovar_gom_setup": "namelist_gom_setup",
    "oovar_gom_setup_hres": "namelist_gom_setup_hres",
}


class OopsVar(Task):
    """Run the OOPS 3D-Var on the merged upper-air ECMA ODB.

    The background is the BlendSur output. The analysis ``ICMSHOOPS+0000`` and
    the updated ``VARBC.cycle`` are archived to ``<da.scratch>/upper_air/oopsvar``.
    """

    def __init__(self, config):
        """Construct OopsVar task.

        Args:
            config (ParsedConfig): Configuration

        """
        Task.__init__(self, config, __class__.__name__)
        self.basetime = as_datetime(config["general.times.basetime"])
        self.da_scratch = config["da.scratch"]
        self.varbc_init_dir = config.get("da.varbc_init_dir", "")
        self.rttov_coef_dir = config["da.rttov_coef_dir"]
        self.bgcycle = config.get("da.bgcycle", "")
        self.obs = Observations(config, da_stream="upper_air")
        self.nlgen = NamelistGenerator(config, "oovar")
        logger.debug("Constructed OopsVar task")

    def execute(self):
        """Run OOVAR and archive the analysis and VarBC coefficients."""
        (yyyy, mm, dd, hh) = split_date(self.basetime)
        da_scratch = self.platform.substitute(self.da_scratch)
        odb_dir = os.path.join(da_scratch, self.obs.family, "odbmerge")
        out_dir = os.path.join(da_scratch, self.obs.family, "oopsvar")
        nproc = int(os.environ.get("NPROC", self.obs.nbpool))

        oovar = self.get_binary("ifs4dvar.DP")
        bindir = os.path.dirname(oovar)
        self.fmanager.input(oovar, "ifs4dvar.DP")
        for tool in ["create_ioassign", "ioassign"]:
            self.fmanager.input(os.path.join(bindir, tool), tool)

        # --- static input data ---
        input_definition = ConfigPaths.path_from_subpath(
            self.platform.substitute("@CYCLE@/oopsvar.json")
        )
        logger.info("Read data spec from: {}", input_definition)
        with open(input_definition, "r", encoding="utf-8") as f:
            input_data = json.load(f)
        self.fmanager.input_data_iterator(input_data)

        # --- background: the blended surface analysis ---
        background = os.path.join(
            da_scratch, "surface", "blendsur", "ICMSHANAL+0000_updated_surface"
        )
        # OOVAR writes the analysis into a copy of the background
        self.fmanager.input(background, "ICMSHOOPS+0000", provider_id="copy")
        for link in [
            "ICMSHMINIIMIN",
            "ICMSHMINIINIT",
            "ICMRFMINI0000",
            "ICMSHOOPSINIT",
            "ICMSHOOPSIMIN",
            "ICMSHSCREINIT",
        ]:
            if not os.path.lexists(link):
                os.symlink("ICMSHOOPS+0000", link)

        # --- VarBC: same cycle of the previous day, else the initial file ---
        self._input_varbc(hh)

        # --- merged ECMA, its subbases and an empty CCMA ---
        if not os.path.isdir(odb_dir):
            raise FileNotFoundError(f"OopsVar: merged ECMA not found at {odb_dir}")
        shutil.copytree(os.path.join(odb_dir, "ECMA"), "ECMA", symlinks=True)
        # The merged IOASSIGN refers to the subbases as ../ECMA.<obstype>
        for entry in os.listdir(odb_dir):
            if entry.startswith("ECMA.") and not os.path.lexists(entry):
                os.symlink(os.path.join(odb_dir, entry), entry)

        os.makedirs("CCMA", exist_ok=True)
        rte_setup = dict(os.environ)
        rte_setup["PATH"] = f"{self.wdir}:{rte_setup.get('PATH', '')}"
        BatchJob(rte_setup, wrapper="").run(f"./create_ioassign -lCCMA -n{nproc}")
        # OOVAR reads one IOASSIGN with both the ECMA and the CCMA entries
        with open("ECMA/IOASSIGN", "a") as ecma_io, open("IOASSIGN") as ccma_io:
            ecma_io.write(ccma_io.read())
        shutil.copy2("ECMA/IOASSIGN", "IOASSIGN")
        if not os.path.isfile("CCMA/IOASSIGN"):
            shutil.copy2("ECMA/IOASSIGN", "CCMA/IOASSIGN")

        # --- namelists and the OOPS configuration ---
        for target, output_file in OOVAR_NAMELISTS.items():
            self.nlgen.generate_namelist(target, output_file)
        # yomlocs.F90 opens namelist_gom_setup_<N>
        os.symlink("namelist_gom_setup", "namelist_gom_setup_0")
        self._write_oops_json(f"{yyyy}-{mm}-{dd}T{hh}:00:00Z")

        # --- environment ---
        rte = {
            **os.environ,
            "ECKIT_MPI_FORCE": "parallel",
            "EC_MEMINFO": "0",
            "EC_LINUX_TRBK": "1",
            "EC_MPI_ATEXIT": "0",
            "EC_PROFILE_HEAP": "0",
            "OOPS_LOGFILE": "oops.log",
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "1"),
            "F_UFMTENDIAN": "big",
            "F_RECLUNIT": "BYTE",
            "MKL_DYNAMIC": "FALSE",
            "DR_HOOK": "0",
            "DR_HOOK_SILENT": "1",
            "DR_HOOK_IGNORE_SIGNALS": "-1",
            "DR_HOOK_ASSERT_MPI_INITIALIZED": "0",
            # hretr_conv.F90 expects YYYYMMDDHH
            "BASETIME": f"{yyyy}{mm}{dd}{hh}",
            "ODB_ANALYSIS_DATE": f"{yyyy}{mm}{dd}",
            "ODB_ANALYSIS_TIME": f"{hh}0000",
            "ODB_STATIC_LINKING": "1",
            "TO_ODB_ECMWF": "0",
            "ODB_TRACE_FILE": "List_odb",
            "TO_ODB_DEBUG": "0",
            "ODB_TRACE_PROC": "0",
            **odb_io_env(self.config, self.obs.nbpool),
            "BATOR_NBSLOT": str(self.obs.bator_nbslot),
            "BATOR_WINDOW_LEN": td2min(self.obs.bator_window_len),
            "BATOR_WINDOW_SHIFT": td2min(self.obs.bator_window_shift),
            "BATOR_SLOT_LEN": td2min(self.obs.bator_slot_len),
            "BATOR_CENTER_LEN": td2min(self.obs.bator_center_len),
            "ODB_CCMA_CREATE_DIRECT": "1",
            "ODB_SRCPATH_RSTBIAS": os.path.join(self.wdir, "ECMA"),
            "ODB_SRCPATH_ECMA": os.path.join(self.wdir, "ECMA"),
            "ODB_DATAPATH_ECMA": os.path.join(self.wdir, "ECMA"),
            "ODB_SRCPATH_CCMA": os.path.join(self.wdir, "CCMA"),
            "ODB_DATAPATH_CCMA": os.path.join(self.wdir, "CCMA"),
            "ODB_CCMA_CREATE_POOLMASK": "1",
            "ODB_CCMA_POOLMASK_FILE": os.path.join(self.wdir, "CCMA", "CCMA.poolmask"),
            "ODB_CMA": "CCMA",
            "IOASSIGN": os.path.join(self.wdir, "IOASSIGN"),
            "RTTOV_COEFDIR": self.platform.substitute(self.rttov_coef_dir),
            "PATH": f"{self.wdir}:{os.environ.get('PATH', '')}",
        }

        # --- run OOVAR ---
        with open("oops.log", "a") as oops_log:
            BatchJob(rte, wrapper=self.platform.substitute(self.wrapper)).run(
                "./ifs4dvar.DP oops.json", logfile=oops_log
            )

        analysis = "ICMSHOOPS+0000"
        if not os.path.isfile(analysis) or os.path.getsize(analysis) == 0:
            raise RuntimeError(f"OopsVar: {analysis} not produced or empty.")

        # --- archive output ---
        tactusmakedirs(out_dir)
        for fname in [analysis, "VARBC.cycle"]:
            self.fmanager.output(fname, os.path.join(out_dir, fname), provider_id="copy")
        logger.info("OopsVar: analysis and VarBC archived to {}", out_dir)
        self.archive_logs(["oops.log", "NODE.001_01"])

    def _input_varbc(self, hh):
        """Link VARBC.cycle of the same cycle on the previous day.

        Falls back to ``da.varbc_init_dir/VARBC.cycle_<HH>``. Only the
        background cycle (``da.bgcycle``) may run without VarBC.

        Args:
            hh (str): Cycle hour

        Raises:
            FileNotFoundError: If no VarBC file is found outside the bgcycle

        """
        previous = self.basetime - timedelta(days=1)
        candidates = [
            os.path.join(
                self.platform.substitute(
                    self.da_scratch, basetime=previous, validtime=previous
                ),
                self.obs.family,
                "oopsvar",
                "VARBC.cycle",
            )
        ]
        if self.varbc_init_dir:
            candidates.append(
                os.path.join(
                    self.platform.substitute(self.varbc_init_dir), f"VARBC.cycle_{hh}"
                )
            )
        for candidate in candidates:
            if os.path.isfile(candidate):
                # OOVAR updates VARBC.cycle in place
                self.fmanager.input(candidate, "VARBC.cycle", provider_id="copy")
                logger.info("OopsVar: VarBC from {}", candidate)
                return
        if self.basetime.strftime("%Y%m%d%H") != self.bgcycle:
            raise FileNotFoundError(f"OopsVar: no VarBC file found in {candidates}")

    def _write_oops_json(self, analysis_time):
        """Write oops.json from oovar_config.yml for this analysis time.

        Args:
            analysis_time (str): Analysis time, ISO 8601

        """
        config_yml = self.nlgen.nlfile.parent / "oovar_config.yml"
        with open(config_yml, encoding="utf-8") as f:
            text = f.read().replace("{analysis_time}", analysis_time)
        with open("oops.json", "w", encoding="utf-8") as f:
            json.dump(yaml.safe_load(text), f, indent=2)

"""Canari task — surface OI analysis with MASTERODB (CANARI, configuration 701)."""

import json
import os
import shutil

from ..config_parser import ConfigPaths
from ..datetime_utils import as_datetime, split_date
from ..initial_conditions import InitialConditions
from ..logs import logger
from ..namelist import NamelistGenerator, write_namelist
from ..obs_utils import Observations, odb_io_env
from ..os_utils import tactusmakedirs
from .base import Task
from .batch import BatchJob

# CANARI predictors, NAMPRE IPRSA(NVNUMAX, JPANAL) (canami.F90, varno_module.F90).
# Without them CAPREDI finds no predictor for any analysis and there is no increment.
_NVNUMAX = 284
_JPANAL = 11
_NMDI = 2147483647
_PREDICTORS = {
    5: 39,  # NAT2: t2m
    6: 58,  # NAH2: rh2m
    9: 11,  # NAST: SST
}


def canari_iprsa():
    """Return NAMPRE IPRSA as a flat list in Fortran (column-major) order.

    The whole array is written rather than IPRSA(1,5)=39 style element
    assignments, which ifort accepted in the namelist read but did not apply.

    Returns:
        list: NVNUMAX * JPANAL predictor VARNO codes, NMDI where unused.

    """
    iprsa = [_NMDI] * (_NVNUMAX * _JPANAL)
    for column, varno in _PREDICTORS.items():
        iprsa[(column - 1) * _NVNUMAX] = varno
    return iprsa


def canari_namelists(config):
    """Write the CANARI namelists fort.4 and EXSEG1.nam to the current directory.

    Args:
        config (ParsedConfig): Configuration

    """
    nlgen = NamelistGenerator(config, "canari")
    nlgen.load("canari")
    nml = nlgen.assemble_namelist("canari")
    if "NAMPRE" not in nml:
        nml["NAMPRE"] = {}
    nml["NAMPRE"]["IPRSA"] = canari_iprsa()
    nml.repeat_counter = True
    write_namelist(nml, "fort.4")

    # SURFEX OI: the forecast's SURFEX configuration plus NAM_NACVEG/NAM_ASSIM
    nlgen_surfex = NamelistGenerator(config, "surfex")
    nlgen_surfex.load("canari")
    settings = nlgen_surfex.assemble_namelist("canari")
    nlgen_surfex.write_namelist(settings, "EXSEG1.nam")


class Canari(Task):
    """Run the CANARI surface OI analysis on the merged surface ECMA ODB.

    The analysis is archived to ``<da.scratch>/surface/canari``.
    """

    def __init__(self, config):
        """Construct Canari task.

        Args:
            config (ParsedConfig): Configuration

        """
        Task.__init__(self, config, __class__.__name__)
        self.basetime = as_datetime(config["general.times.basetime"])
        self.da_scratch = config["da.scratch"]
        self.obs = Observations(config)
        logger.debug("Constructed Canari task")

    def execute(self):
        """Run CANARI and archive the surface analysis."""
        (yyyy, mm, dd, hh) = split_date(self.basetime)
        da_scratch = self.platform.substitute(self.da_scratch)
        odb_dir = os.path.join(da_scratch, self.obs.family, "odbmerge")

        masterodb = self.get_binary("MASTERODB")
        self.fmanager.input(masterodb, "MASTERODB")

        # --- namelists ---
        canari_namelists(self.config)

        # --- static input data and climate files ---
        # The climate file of the neighbouring month in time is read as well
        mm2 = int(mm) - 1 if int(dd) <= 15 else int(mm) + 1
        mm2 = (mm2 - 1) % 12 + 1
        self.platform.add_macro({"MM2": f"{mm2:02d}"})
        input_definition = ConfigPaths.path_from_subpath(
            self.platform.substitute("@CYCLE@/canari.json")
        )
        logger.info("Read data spec from: {}", input_definition)
        with open(input_definition, "r", encoding="utf-8") as f:
            input_data = json.load(f)
        self.fmanager.input_data_iterator(input_data)

        # --- first guess ---
        fg_atm, fg_sfx, _ = InitialConditions(self.config).find_initial_files(
            "Canari", fail=True
        )
        for link in [
            "ICMSHCYCLINIT",
            "ICMGGCYCLINIT",
            "ELSCFCYCLALBC000",
            "ELSCFANALALBC000",
            "ICMSHANALINIT",
        ]:
            self.fmanager.input(fg_atm, link)
        self.fmanager.input(fg_sfx, "ICMSHCYCLINIT.sfx")
        self.fmanager.input(fg_sfx, "ICMSHANALINIT.sfx", provider_id="copy")
        # The analysis is written into copies of the first guess. MASTERODB then
        # only rewrites the fields it analyses, so the hydrometeors, TKE and the
        # non-hydrostatic fields of the guess survive in ICMSHANAL+0000.
        self.fmanager.input(fg_atm, "ICMSHANAL+0000", provider_id="copy")
        self.fmanager.input(fg_sfx, "ICMSHANAL+0000.sfx", provider_id="copy")

        # --- merged ECMA and its subbases (CANARI updates them) ---
        if not os.path.isdir(odb_dir):
            raise FileNotFoundError(f"Canari: merged ECMA not found at {odb_dir}")
        for entry in sorted(os.listdir(odb_dir)):
            src = os.path.join(odb_dir, entry)
            if os.path.isdir(src) and not os.path.exists(entry):
                shutil.copytree(src, entry, symlinks=True)
        for link in ["IOASSIGN", "IOASSIGN.ECMA"]:
            if not os.path.lexists(link):
                os.symlink(os.path.join("ECMA", "ECMA.IOASSIGN"), link)

        # --- ODB environment ---
        rte = {
            **os.environ,
            "TO_ODB_ECMWF": "0",
            "TO_ODB_SWAPOUT": "0",
            "ODB_DEBUG": "0",
            "ODB_CTX_DEBUG": "0",
            "ODB_REPRODUCIBLE_SEQNO": "4",
            "ODB_STATIC_LINKING": "1",
            **odb_io_env(self.config, self.obs.nbpool),
            "EC_PROFILE_HEAP": "0",
            "TRACEBK": "0",
            "ODB_ANALYSIS_DATE": f"{yyyy}{mm}{dd}",
            "ODB_ANALYSIS_TIME": f"{hh}0000",
            "TIME_INIT_YYYYMMDD": f"{yyyy}{mm}{dd}",
            "TIME_INIT_HHMMSS": f"{hh}0000",
            "ODB_FEBINPATH": os.path.dirname(masterodb),
            "ODB_CMA": "ECMA",
            "ODB_SRCPATH_ECMA": os.path.join(self.wdir, "ECMA"),
            "ODB_DATAPATH_ECMA": os.path.join(self.wdir, "ECMA"),
            "ODB_MERGEODB_DIRECT": "0",
            "BASETIME": f"{yyyy}{mm}{dd}{hh}",
            "CNMEXPB": "CYCL",
            "F_RECLUNIT": "BYTE",
            "F_UFMTENDIAN": "big:10,33,50,54,81",
            # The poolmask is not needed by CANARI, and creating it crashes on
            # empty pools (index.body.len = NMDI) left by the ingestion.
            "ODB_ECMA_CREATE_POOLMASK": "0",
            "ODB_ECMA_POOLMASK_FILE": os.path.join(self.wdir, "ECMA", "ECMA.poolmask"),
            "IOASSIGN": "IOASSIGN",
            # MPI stubs linked into the binary make DrHook believe that MPI
            # was never initialised.
            "DR_HOOK_ASSERT_MPI_INITIALIZED": "0",
        }

        # --- run MASTERODB ---
        BatchJob(rte, wrapper=self.platform.substitute(self.wrapper)).run("./MASTERODB")

        output_file = "ICMSHANAL+0000"
        if not os.path.isfile(output_file) or os.path.getsize(output_file) == 0:
            raise RuntimeError(f"Canari: {output_file} not produced or empty.")

        # --- archive output ---
        out_dir = os.path.join(da_scratch, self.obs.family, "canari")
        tactusmakedirs(out_dir)
        for fname in [output_file, f"{output_file}.sfx"]:
            self.fmanager.output(fname, os.path.join(out_dir, fname), provider_id="copy")
        logger.info("Canari: analysis archived to {}", out_dir)
        self.archive_logs(["fort.4", "EXSEG1.nam", "NODE.001_01"])

"""BlendSur task — blend the CANARI surface analysis with the SST of the LBC.

Two BLENDSUR runs:
  1. Add the LBC SST over sea to the CANARI analysis.
  2. Subtract the CANARI SST again: land surface from CANARI, sea from the LBC.

The result is the background for OopsVar and the initial file of the Forecast.
"""

import os

from ..datetime_utils import as_datetime
from ..logs import logger
from ..namelist import NamelistGenerator
from ..obs_utils import Observations
from ..os_utils import tactusmakedirs
from .base import Task
from .batch import BatchJob


class BlendSur(Task):
    """Blend the CANARI surface analysis with the LBC SST using BLENDSUR.

    The result is archived to ``<da.scratch>/surface/blendsur``.
    """

    def __init__(self, config):
        """Construct BlendSur task.

        Args:
            config (ParsedConfig): Configuration

        """
        Task.__init__(self, config, __class__.__name__)
        self.basetime = as_datetime(config["general.times.basetime"])
        self.da_scratch = config["da.scratch"]
        self.intp_bddir = config["system.intp_bddir"]
        self.lbc_template = config["file_templates.interpolated_boundaries.archive"]
        self.obs = Observations(config)
        self.nlgen = NamelistGenerator(config, "blendsur")
        logger.debug("Constructed BlendSur task")

    def execute(self):
        """Run the two BLENDSUR steps and archive the blended analysis."""
        da_scratch = self.platform.substitute(self.da_scratch)
        canari_dir = os.path.join(da_scratch, self.obs.family, "canari")

        self.fmanager.input(self.get_binary("BLENDSUR"), "BLENDSUR")

        # BLENDSUR writes into its output file, which must already exist
        lbc = self.platform.substitute(
            f"{self.intp_bddir}/{self.lbc_template}",
            validtime=self.basetime,
            bd_index=0,
        )
        canari_analysis = os.path.join(canari_dir, "ICMSHANAL+0000")
        self.fmanager.input(lbc, "AL_LBC", provider_id="copy")
        for fname in ["AL_3D", "AL_2S", "AL_A"]:
            self.fmanager.input(canari_analysis, fname, provider_id="copy")

        rte = dict(os.environ)
        for step in ["blendsur_step1", "blendsur_step2"]:
            self.nlgen.generate_namelist(step, "fort.4")
            BatchJob(rte, wrapper="").run("./BLENDSUR", logfile=f"{step}.log")
            with open(f"{step}.log", encoding="utf-8") as fh:
                if "validity times are not the same" in fh.read():
                    logger.warning(
                        "BlendSur: BLENDSUR skipped {} (validity times differ)", step
                    )

        # --- archive output ---
        out_dir = os.path.join(da_scratch, self.obs.family, "blendsur")
        tactusmakedirs(out_dir)
        output_file = os.path.join(out_dir, "ICMSHANAL+0000_updated_surface")
        self.fmanager.output("AL_A", output_file, provider_id="copy")
        logger.info("BlendSur: blended analysis archived to {}", output_file)
        self.archive_logs(["blendsur_step1.log", "blendsur_step2.log"])

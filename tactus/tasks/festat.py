"""Festat task - Jb structure functions from downscaled ensemble forecasts.
"""

import os
import re

from ..datetime_utils import as_datetime, as_timedelta
from ..eps.eps_setup import get_member_config
from ..logs import logger
from ..namelist import NamelistGenerator
from ..os_utils import tactusmakedirs
from .base import Task
from .batch import BatchJob

# Festat namelist targets and the files they are written to
FESTAT_NAMELISTS = {
    "festat_fort4": "fort.4",
}


class Festat(Task):
    """Run the FA-reading FESTAT on the member forecasts of all cycles.
    """

    def __init__(self, config):
        """Construct Festat task.

        Args:
            config (ParsedConfig): Configuration

        """
        Task.__init__(self, config, __class__.__name__)

        self.domain = config["domain.name"]
        self.vlev = int(config["vertical_levels.nlev"])
        self.dtgbeg = as_datetime(
            config.get("jb.dtgbeg", config["general.times.start"])
        ).strftime("%Y%m%d")
        self.members = config["eps.general.members"]
        self.start = as_datetime(config["general.times.start"])
        self.end = as_datetime(config["general.times.end"])
        self.step = as_timedelta(config.get("general.times.cycle_length", "PT3H"))
        self.case_lead = as_timedelta(
            config.get("jb.case_lead", config["general.times.forecast_range"])
        )
        self.fc_dir = config["jb.fc_dir"]
        self.fc_file = config.get("jb.fc_file", "ICMSH@CNMEXP@+@FCLENGTH@h00m00s")
        self.case_prefix = config.get("jb.case_prefix", "ICMSH@CNMEXP@+")
        self.cname = config.get("jb.cname", "ICMSH@CNMEXP@+0")
        self.output_dir = config["jb.output_dir"]
        self.publish_stabal96 = config.get("jb.publish_stabal96", True)

        self.nlgen = NamelistGenerator(config, "festat")
        logger.debug("Constructed Festat task")

    def execute(self):
        """Collect the member forecasts, run FESTAT, archive statistics."""
        # --- input cases: resolve and link as <case_prefix>NNN ---
        self.case_prefix = self.case_prefix.replace("@CNMEXP@", self.config["general.cnmexp"])
        case_files = self._collect_cases()
        for n, fname in enumerate(case_files, start=1):
            target = f"{self.case_prefix}{n:04d}"
            if not os.path.lexists(target):
                os.symlink(fname, target)
        ncases = len(case_files)
        logger.info("Festat: using {} input cases", ncases)

        if ncases <= self.vlev:
            raise ValueError(
                f"Festat: {ncases} input cases is not enough; more than "
                f"the number of vertical levels ({self.vlev}) are required."
            )

        # --- output file names ---
        stem = f"stab_{self.domain}_{self.vlev}_{self.dtgbeg}_{ncases}"
        out_bal = f"{stem}.bal"
        out_cvt = f"{stem}.cvt"
        out_cv = f"{stem}.cv"

        # --- namelist ---
        for target, output_file in FESTAT_NAMELISTS.items():
            self.nlgen.generate_namelist(target, output_file)
        self._patch_fort4(ncases, out_bal, out_cvt, out_cv)

        # --- binary and environment ---
        festat = self.get_binary("FESTAT")
        self.fmanager.input(festat, "FESTAT")

        nproc = int(
            os.environ.get(
                "NPROC", self.config.get("submission.task_exceptions.Festat.NPROC", 16)
            )
        )
        nprocx = int(
            os.environ.get(
                "NPROCX", self.config.get("submission.task_exceptions.Festat.NPROCX", 1)
            )
        )
        nprocy = int(
            os.environ.get(
                "NPROCY", self.config.get("submission.task_exceptions.Festat.NPROCY", 1)
            )
        )
        omp_threads = int(
            os.environ.get(
                "OMP_NUM_THREADS",
                self.config.get(
                    "submission.task_exceptions.Festat.OMP_NUM_THREADS", 1
                ),
            )
        )
        # The processor grid and the thread count are read from the
        # job environment, which the submission layer populates from
        # the task exceptions, so the parallel configuration of the
        # case reaches the binary unmodified; the defaults keep the
        # task runnable as a plain serial job
        rte = {
            **os.environ,
            "OMP_NUM_THREADS": str(omp_threads),
            "NPROC": str(nproc),
            "NPROCX": str(nprocx),
            "NPROCY": str(nprocy),
        }

        # --- run FESTAT ---
        with open("festat.log", "a") as festat_log:
            BatchJob(rte, wrapper=self.platform.substitute(self.wrapper)).run(
                "./FESTAT", logfile=festat_log
            )

        # --- verify and archive output ---
        outputs = [out_bal, out_cvt, out_cv]
        for fname in outputs:
            if not os.path.isfile(fname) or os.path.getsize(fname) == 0:
                raise RuntimeError(f"Festat: {fname} not produced or empty.")

        out_dir = self.platform.substitute(self.output_dir)
        tactusmakedirs(out_dir)
        for fname in outputs:
            self.fmanager.output(fname, os.path.join(out_dir, fname), provider_id="copy")
        if self.publish_stabal96:
            for fname in outputs:
                ext = os.path.splitext(fname)[1]
                link = os.path.join(out_dir, f"stabal96{ext}")
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(fname, link)
            logger.info(
                "Festat: stabal96 links published in {}", out_dir
            )
        self.archive_logs(["festat.log", "NODE.001_01"])
        logger.info("Festat: structure functions archived to {}", out_dir)

    def _collect_cases(self):
        """Resolve the member forecast files of all cycles.

        Returns:
            list: Absolute paths of the input case files, ordered by
                cycle and member

        Raises:
            FileNotFoundError: If member forecast files are missing; the
                error lists the first missing file of the full set

        """
        fc_hour = int(self.case_lead.total_seconds() // 3600)
        case_files = []
        missing = []
        basetime = self.start
        while basetime <= self.end:
            for member in self.members:
                member_config = get_member_config(self.config, member)
                member_str = member_config["general.member_str"]
                fc_dir = self.platform.substitute(
                    self.fc_dir.replace("@MEMBER_STR@", member_str),
                    validtime=basetime,
                )
                source = self.fc_file.replace(
                    "@CNMEXP@", self.config["general.cnmexp"]
                ).replace("@FCLENGTH@", f"{fc_hour:04d}")
                source = os.path.join(fc_dir, source)
                if os.path.isfile(source):
                    case_files.append(os.path.realpath(source))
                else:
                    missing.append(source)
            basetime += self.step
        if missing:
            raise FileNotFoundError(
                f"Festat: {len(missing)} of {len(case_files) + len(missing)} "
                f"member forecasts missing; first missing: {missing[0]}"
            )
        return case_files

    def _patch_fort4(self, ncases, out_bal, out_cvt, out_cv):
        """Patch NCASES, the OUT* names and CNAME in the generated fort.4.

        Args:
            ncases (int): Number of input cases
            out_bal (str): Balance operators output file
            out_cvt (str): Vertical covariance transform output file
            out_cv (str): Covariance output file

        Raises:
            ValueError: If a key is missing from the generated namelist

        """
        cname = self.platform.substitute(self.cname)
        with open("fort.4", "r", encoding="utf-8") as f:
            text = f.read()
        patches = {
            "NCASES": str(ncases),
            "CNAME": f"'{cname}'",
            "OUTBAL": f"'{out_bal}'",
            "OUTCVT": f"'{out_cvt}'",
            "OUTCVU": f"'{out_cv}'",
        }
        for key, value in patches.items():
            text, nsub = re.subn(
                rf"^\s*{key}\s*=.*$",
                f"  {key}={value},",
                text,
                count=1,
                flags=re.MULTILINE,
            )
            if nsub != 1:
                raise ValueError(f"Festat: key {key} missing from NAMFESTAT")
        with open("fort.4", "w", encoding="utf-8") as f:
            f.write(text)
        logger.info("Festat: NCASES={}, CNAME={} patched into fort.4", ncases, cname)
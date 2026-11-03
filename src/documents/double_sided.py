import datetime as dt
import logging
from pathlib import Path
from typing import Final
from uuid import uuid4

from django.conf import settings
from filelock import FileLock

from documents.converters import convert_from_tiff_to_pdf
from documents.exceptions import ConsumerError
from documents.jobs import _copy
from documents.jobs import _sync_directory
from documents.plugins.base import ConsumeTaskPlugin
from documents.plugins.base import NoCleanupPluginMixin
from documents.plugins.base import NoSetupPluginMixin
from documents.plugins.base import StopConsumeTaskError

logger = logging.getLogger("paperless.double_sided")

# Hardcoded for now, could be made a configurable setting if needed
TIMEOUT_MINUTES: Final[int] = 30
TIMEOUT_SECONDS: Final[int] = TIMEOUT_MINUTES * 60

# Used by test cases
STAGING_FILE_NAME = "double-sided-staging.pdf"


class CollatePlugin(NoCleanupPluginMixin, NoSetupPluginMixin, ConsumeTaskPlugin):
    NAME: str = "CollatePlugin"

    @property
    def able_to_run(self) -> bool:
        return (
            settings.CONSUMER_ENABLE_COLLATE_DOUBLE_SIDED
            and settings.CONSUMER_COLLATE_DOUBLE_SIDED_SUBDIR_NAME
            in (self.input_doc.source_path or self.input_doc.original_file).parts
        )

    def run(self) -> str | None:
        """
        Tries to collate pages from 2 single sided scans of a double sided
        document.

        When called with a file, it checks whether or not a staging file
        exists, if not, the current file is turned into that staging file
        containing the odd numbered pages.

        If a staging file exists, and it is not too old, the current file is
        considered to be the second part (the even numbered pages) and it will
        collate the pages of both, the pages of the second file will be added
        in reverse order, since the ADF will have scanned the pages from bottom
        to top.

        Returns a status message on success, or raises a ConsumerError
        in case of failure.
        """

        directory = (
            settings.DATA_DIR / "jobs" / "collation"
            if self.input_doc.job_id
            else settings.SCRATCH_DIR
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with FileLock(directory / "collation.lock"):
            return self._collate(directory / STAGING_FILE_NAME)

    def _collate(self, staging: Path) -> str | None:
        """Publish a durable merged file before releasing either half scan."""
        if self.input_doc.mime_type == "application/pdf":
            pdf_file = self.input_doc.original_file
        elif (
            self.input_doc.mime_type == "image/tiff"
            and settings.CONSUMER_COLLATE_DOUBLE_SIDED_TIFF_SUPPORT
        ):
            pdf_file = convert_from_tiff_to_pdf(
                self.input_doc.original_file,
                self.base_tmp_dir,
            )
        else:
            raise ConsumerError(
                "Unsupported file type for collation of double-sided scans",
            )

        valid_staging_exists = False
        if staging.exists():
            stats = staging.stat()
            # if the file is older than the timeout, we don't consider
            # it valid
            if (dt.datetime.now().timestamp() - stats.st_mtime) > TIMEOUT_SECONDS:
                expired = staging.with_name(f"expired-{uuid4()}.pdf")
                staging.replace(expired)
                _sync_directory(staging.parent)
                logger.warning("Retained expired half scan for recovery at %s", expired)
            else:
                valid_staging_exists = True

        if valid_staging_exists:
            from pikepdf import Pdf

            try:
                # Collate pages from second PDF in reverse order
                with Pdf.open(staging) as pdf1, Pdf.open(pdf_file) as pdf2:
                    pdf2.pages.reverse()
                    try:
                        for i, page in enumerate(pdf2.pages):
                            pdf1.pages.insert(2 * i + 1, page)
                    except IndexError:
                        raise ConsumerError(
                            "This second file (even numbered pages) contains more "
                            "pages than the first/odd numbered one. This means the "
                            "two uploaded files don't belong to the same double-"
                            "sided scan. Please retry, starting with the odd "
                            "numbered pages again.",
                        )
                    # Merged file has the same path, but without the
                    # double-sided subdir. Therefore, it is also in the
                    # consumption dir and will be picked up for processing
                    old_file = (
                        self.input_doc.source_path or self.input_doc.original_file
                    )
                    new_file = Path(
                        *(
                            part
                            for part in old_file.with_name(
                                f"{old_file.stem}-collated.pdf",
                            ).parts
                            if part
                            != settings.CONSUMER_COLLATE_DOUBLE_SIDED_SUBDIR_NAME
                        ),
                    )
                    # If the user didn't create the subdirs yet, do it for them
                    new_file.parent.mkdir(parents=True, exist_ok=True)
                    if new_file.exists():
                        new_file = new_file.with_stem(f"{new_file.stem}-{uuid4()}")
                    merged = self.base_tmp_dir / "collated.pdf"
                    pdf1.save(merged)
                _copy(merged, new_file)
            except Exception:
                logger.exception("Collation failed; both half scans remain recoverable")
                raise
            else:
                staging.unlink()
                _sync_directory(staging.parent)
                pdf_file.unlink()
                self.input_doc.original_file.unlink(missing_ok=True)
                logger.info("Collated documents into new file %s", new_file)
                raise StopConsumeTaskError(
                    "Success. Even numbered pages of double sided scan collated "
                    "with odd pages",
                )
        else:
            _copy(pdf_file, staging)
            pdf_file.unlink()
            self.input_doc.original_file.unlink(missing_ok=True)
            logger.info(
                "Got scan with odd numbered pages of double-sided scan, moved it to %s",
                staging,
            )
            raise StopConsumeTaskError(
                "Received odd numbered pages of double sided scan, waiting up to "
                f"{TIMEOUT_MINUTES} minutes for even numbered pages",
            )

import logging
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone
from filelock import FileLock

from documents.caching import clear_document_caches
from documents.models import Document
from documents.templating.utils import convert_format_str_to_template_format
from documents.utils import compute_checksum

logger = logging.getLogger("paperless.filehandling")


class UnsafeFilePathError(Exception):
    """
    Raised when a path generated for a document would land outside of its root.
    """


def validate_path_in_root(path: Path, root: Path) -> None:
    """
    Ensures the given absolute path is contained within root, the
    equivalent guard for the later move.
    """
    if not path.resolve().is_relative_to(root.resolve()):
        msg = f"Refusing to write file outside of root {root}: {path}."
        logger.warning(msg)
        raise UnsafeFilePathError(msg)


def create_source_path_directory(source_path: Path) -> None:
    source_path.parent.mkdir(parents=True, exist_ok=True)


def delete_empty_directories(directory: Path, root: Path) -> None:
    if not directory.is_dir():
        return

    if not directory.is_relative_to(root):
        # don't do anything outside our originals folder.

        # append os.path.set so that we avoid these cases:
        #   directory = /home/originals2/test
        #   root = /home/originals ("/" gets appended and startswith fails)
        return

    # Go up in the directory hierarchy and try to delete all directories
    while directory != root:
        if not list(directory.iterdir()):
            # it's empty
            try:
                directory.rmdir()
            except OSError:
                # whatever. empty directories aren't that bad anyway.
                return
        else:
            # it's not empty.
            return

        # go one level up
        directory = directory.parent


def generate_unique_filename(doc, *, archive_filename=False) -> Path:
    """
    Generates a unique filename for doc in settings.ORIGINALS_DIR.

    The returned filename is guaranteed to be either the current filename
    of the document if unchanged, or a new filename that does not correspondent
    to any existing files. The function will append _01, _02, etc to the
    filename before the extension to avoid conflicts.

    If archive_filename is True, return a unique archive filename instead.

    """
    if archive_filename:
        old_filename: Path | None = (
            Path(doc.archive_filename) if doc.archive_filename else None
        )
        root = settings.ARCHIVE_DIR
    else:
        old_filename = Path(doc.filename) if doc.filename else None
        root = settings.ORIGINALS_DIR

    base_filename = generate_filename(doc, archive_filename=archive_filename)

    # If generating archive filenames, try to make a name that is similar to
    # the original filename first.

    if archive_filename and doc.filename:
        # Try to create a simple PDF version based on the original filename
        # but preserve any directory structure from the template
        if str(base_filename.parent) != ".":
            # Has directory structure, preserve it
            simple_pdf_name = base_filename.parent / (Path(doc.filename).stem + ".pdf")
        else:
            # No directory structure
            simple_pdf_name = Path(Path(doc.filename).stem + ".pdf")

        if simple_pdf_name == old_filename or not (root / simple_pdf_name).exists():
            return simple_pdf_name

    file_extension = ".pdf" if archive_filename else doc.file_type
    filename_stem = base_filename.name.removesuffix(file_extension)
    counter = 0

    while True:
        new_filename = base_filename
        if counter:
            new_filename = base_filename.with_name(
                f"{filename_stem}_{counter:02}{file_extension}",
            )

        if new_filename == old_filename:
            # still the same as before.
            return new_filename

        if (root / new_filename).exists():
            counter += 1
        else:
            return new_filename


def format_filename(document: Document, template_str: str) -> str | None:
    from documents.templating.filepath import is_safe_relative_path
    from documents.templating.filepath import validate_filepath_template_and_render

    rendered_filename = validate_filepath_template_and_render(
        template_str,
        document,
    )
    if rendered_filename is None:
        return None

    # Apply this setting.  It could become a filter in the future (or users could use |default)
    if settings.FILENAME_FORMAT_REMOVE_NONE:
        rendered_filename = rendered_filename.replace("/-none-/", "/")
        rendered_filename = rendered_filename.replace(" -none-", "")
        rendered_filename = rendered_filename.replace("-none-", "")
        rendered_filename = rendered_filename.strip(os.sep)

    rendered_filename = rendered_filename.replace(
        "-none-",
        "none",
    )  # backward compatibility

    # Validate again after remove none
    if not is_safe_relative_path(rendered_filename):
        logger.warning(
            "Filename became unsafe after placeholder removal, "
            "falling back to default naming",
        )
        return None

    return rendered_filename


def generate_filename(
    doc: Document,
    *,
    counter=0,
    archive_filename=False,
    use_format=True,
) -> Path:
    # version docs use the root document for formatting, just with a suffix
    context_doc = doc if doc.root_document_id is None else doc.root_document
    version_suffix = (
        f"_v{doc.version_index}"
        if doc.root_document_id is not None and doc.version_index is not None
        else ""
    )
    base_path: Path | None = None

    # Determine the source of the format string
    if use_format:
        if context_doc.storage_path is not None:
            filename_format = context_doc.storage_path.path
        elif settings.FILENAME_FORMAT is not None:
            # Maybe convert old to new style
            filename_format = convert_format_str_to_template_format(
                settings.FILENAME_FORMAT,
            )
        else:
            filename_format = None
    else:
        filename_format = None

    # If we have one, render it
    if filename_format is not None:
        rendered_path: str | None = format_filename(context_doc, filename_format)
        if rendered_path:
            base_path = Path(rendered_path)

    counter_str = f"_{counter:02}" if counter else ""
    filetype_str = ".pdf" if archive_filename else doc.file_type

    if base_path:
        # Split the path into directory and filename parts
        directory = base_path.parent
        # Use the full name (not just stem) as the base filename
        base_filename = base_path.name

        # Build the final filename with counter and filetype
        final_filename = f"{base_filename}{version_suffix}{counter_str}{filetype_str}"

        # If we have a directory component, include it
        if str(directory) != ".":
            full_path = directory / final_filename
        else:
            full_path = Path(final_filename)
    else:
        # No template, use document ID
        final_filename = (
            f"{context_doc.pk:07}{version_suffix}{counter_str}{filetype_str}"
        )
        full_path = Path(final_filename)

    return full_path


class CannotMoveFilesException(Exception):
    """A file move cannot preserve the document's existing storage guarantees."""


def _path_matches_checksum(path: Path, checksum: str | None) -> bool:
    if checksum is None or not path.is_file():
        return False

    return compute_checksum(path) == checksum


def filename_uses_custom_fields(doc: Document) -> bool:
    """Return whether the effective filename template references custom fields."""
    template = None
    if doc.storage_path is not None:
        template = doc.storage_path.path
    elif settings.FILENAME_FORMAT is not None:
        template = convert_format_str_to_template_format(settings.FILENAME_FORMAT)

    if not template:
        return False

    return "custom_fields" in template


def reconcile_document_files(instance: Document) -> None:
    """Reconcile stored filenames under the media lock, recovering failed moves.

    Call after metadata persistence. Existing path checks, collision handling,
    checksum recovery and rollback preserve original and archive files. Handled
    failures leave metadata saved and keep the previous filenames.
    """

    def validate_move(instance, old_path: Path, new_path: Path, root: Path) -> None:
        if not new_path.is_relative_to(root):
            msg = (
                f"Document {instance!s}: Refusing to move file outside root {root}: "
                f"{new_path}."
            )
            logger.warning(msg)
            raise CannotMoveFilesException(msg)

        if not old_path.is_file():
            # Can't do anything if the old file does not exist anymore.
            msg = f"Document {instance!s}: File {old_path} doesn't exist."
            logger.fatal(msg)
            raise CannotMoveFilesException(msg)

        if new_path.is_file():
            # Can't do anything if the new file already exists. Skip updating file.
            msg = f"Document {instance!s}: Cannot rename file since target path {new_path} already exists."
            logger.warning(msg)
            raise CannotMoveFilesException(msg)

    if not instance.filename:
        # Can't update the filename if there is no filename to begin with
        # This happens when the consumer creates a new document.
        # The document is modified and saved multiple times, and only after
        # everything is done (i.e., the generated filename is final),
        # filename will be set to the location where the consumer has put
        # the file.
        #
        # This will in turn cause this logic to move the file where it belongs.
        return

    with FileLock(settings.MEDIA_LOCK):
        # If this was waiting for the lock, the filename or archive_filename
        # of this document may have been updated.  This happens if multiple updates
        # get queued from the UI for the same document
        # So freshen up the data before doing anything
        instance.refresh_from_db()

        try:
            old_filename = instance.filename
            old_source_path = instance.source_path
            move_original = False
            original_moved = False
            original_already_moved = False

            old_archive_filename = instance.archive_filename
            old_archive_path = instance.archive_path
            move_archive = False
            archive_moved = False
            archive_already_moved = False

            candidate_filename = generate_filename(instance)
            if len(str(candidate_filename)) > Document.MAX_STORED_FILENAME_LENGTH:
                msg = (
                    f"Document {instance!s}: Generated filename exceeds db path "
                    f"limit ({len(str(candidate_filename))} > "
                    f"{Document.MAX_STORED_FILENAME_LENGTH}): {candidate_filename!s}"
                )
                logger.warning(msg)
                raise CannotMoveFilesException(msg)

            candidate_source_path = (
                settings.ORIGINALS_DIR / candidate_filename
            ).resolve()
            if candidate_filename == Path(old_filename):
                new_filename = Path(old_filename)
            elif (
                candidate_source_path.exists()
                and candidate_source_path != old_source_path
            ):
                if not old_source_path.is_file() and _path_matches_checksum(
                    candidate_source_path,
                    instance.checksum,
                ):
                    new_filename = candidate_filename
                    original_already_moved = True
                else:
                    # Only fall back to unique search when there is an actual conflict
                    new_filename = generate_unique_filename(instance)
            else:
                new_filename = candidate_filename

            # Need to convert to string to be able to save it to the db
            instance.filename = str(new_filename)
            move_original = (
                old_filename != instance.filename and not original_already_moved
            )

            if instance.has_archive_version:
                archive_candidate = generate_filename(instance, archive_filename=True)
                if len(str(archive_candidate)) > Document.MAX_STORED_FILENAME_LENGTH:
                    msg = (
                        f"Document {instance!s}: Generated archive filename exceeds "
                        f"db path limit ({len(str(archive_candidate))} > "
                        f"{Document.MAX_STORED_FILENAME_LENGTH}): {archive_candidate!s}"
                    )
                    logger.warning(msg)
                    raise CannotMoveFilesException(msg)
                archive_candidate_path = (
                    settings.ARCHIVE_DIR / archive_candidate
                ).resolve()
                if archive_candidate == Path(old_archive_filename):
                    new_archive_filename = Path(old_archive_filename)
                elif (
                    archive_candidate_path.exists()
                    and archive_candidate_path != old_archive_path
                ):
                    if not old_archive_path.is_file() and _path_matches_checksum(
                        archive_candidate_path,
                        instance.archive_checksum,
                    ):
                        new_archive_filename = archive_candidate
                        archive_already_moved = True
                    else:
                        new_archive_filename = generate_unique_filename(
                            instance,
                            archive_filename=True,
                        )
                else:
                    new_archive_filename = archive_candidate

                instance.archive_filename = str(new_archive_filename)

                move_archive = (
                    old_archive_filename != instance.archive_filename
                    and not archive_already_moved
                )
            else:
                move_archive = False

            if not move_original and not move_archive:
                updates = {"modified": timezone.now()}
                if old_filename != instance.filename:
                    updates["filename"] = instance.filename
                if old_archive_filename != instance.archive_filename:
                    updates["archive_filename"] = instance.archive_filename

                # Don't save() here to prevent infinite recursion.
                Document.objects.filter(pk=instance.pk).update(**updates)
                return

            if move_original:
                validate_move(
                    instance,
                    old_source_path,
                    instance.source_path,
                    settings.ORIGINALS_DIR,
                )
                create_source_path_directory(instance.source_path)
                shutil.move(old_source_path, instance.source_path)
                original_moved = True

            if move_archive:
                validate_move(
                    instance,
                    old_archive_path,
                    instance.archive_path,
                    settings.ARCHIVE_DIR,
                )
                create_source_path_directory(instance.archive_path)
                shutil.move(old_archive_path, instance.archive_path)
                archive_moved = True

            # Don't save() here to prevent infinite recursion.
            Document.global_objects.filter(pk=instance.pk).update(
                filename=instance.filename,
                archive_filename=instance.archive_filename,
                modified=timezone.now(),
            )
            # Clear any caching for this document.  Slightly overkill, but not terrible
            clear_document_caches(instance.pk)

        except (OSError, DatabaseError, CannotMoveFilesException) as e:
            logger.warning(f"Exception during file handling: {e}")
            # This happens when either:
            #  - moving the files failed due to file system errors
            #  - saving to the database failed due to database errors
            # In both cases, we need to revert to the original state.

            # Only restore moves this call completed; a rejected target may be unrelated.
            for moved, current_path, previous_path in (
                (original_moved, instance.source_path, old_source_path),
                (archive_moved, instance.archive_path, old_archive_path),
            ):
                if not moved:
                    continue
                try:
                    if previous_path.exists() or previous_path.is_symlink():
                        raise FileExistsError(previous_path)
                    shutil.move(current_path, previous_path)
                except Exception:
                    logger.exception(
                        "Document %s: could not restore %s to %s; manual file recovery required",
                        instance.pk,
                        current_path,
                        previous_path,
                    )

            # restore old values on the instance
            instance.filename = old_filename
            instance.archive_filename = old_archive_filename

        # finally, remove any empty sub folders. This will do nothing if
        # something has failed above.
        if not old_source_path.is_file():
            delete_empty_directories(
                Path(old_source_path).parent,
                root=settings.ORIGINALS_DIR,
            )

        if instance.has_archive_version and not old_archive_path.is_file():
            delete_empty_directories(
                Path(old_archive_path).parent,
                root=settings.ARCHIVE_DIR,
            )

    # Keep version files in sync with root
    if instance.root_document_id is None:
        for version_doc in Document.objects.filter(root_document_id=instance.pk).only(
            "pk",
        ):
            reconcile_document_files(version_doc)

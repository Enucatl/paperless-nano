"""Backfill unambiguous language tags without deleting tags or saved filters."""

from django.db import migrations
from django.db import models

LANGUAGE_CHOICES = (
    ("af", "Afrikaans"),
    ("am", "Amharic"),
    ("apc", "Levantine Arabic"),
    ("apd", "Sudanese Arabic"),
    ("arb", "Modern Standard Arabic"),
    ("arq", "Algerian Arabic"),
    ("ary", "Moroccan Arabic"),
    ("arz", "Egyptian Arabic"),
    ("bg", "Bulgarian"),
    ("bho", "Bhojpuri"),
    ("bn", "Bengali"),
    ("ca", "Catalan"),
    ("cjy", "Jinyu Chinese"),
    ("cmn", "Mandarin Chinese"),
    ("cs", "Czech"),
    ("cy", "Welsh"),
    ("da", "Danish"),
    ("de", "Standard German"),
    ("el", "Greek"),
    ("en", "English"),
    ("es", "Spanish"),
    ("et", "Estonian"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("gu", "Gujarati"),
    ("ha", "Hausa"),
    ("hak", "Hakka Chinese"),
    ("he", "Hebrew"),
    ("hi", "Hindi"),
    ("hr", "Croatian"),
    ("hsn", "Xiang Chinese"),
    ("hu", "Hungarian"),
    ("id", "Indonesian"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("jv", "Javanese"),
    ("kn", "Kannada"),
    ("ko", "Korean"),
    ("ln", "Lingala"),
    ("lt", "Lithuanian"),
    ("lv", "Latvian"),
    ("mk", "Macedonian"),
    ("ml", "Malayalam"),
    ("mr", "Marathi"),
    ("my", "Burmese"),
    ("nan", "Min Nan Chinese"),
    ("ne", "Nepali"),
    ("nl", "Dutch"),
    ("no", "Norwegian"),
    ("or", "Odia"),
    ("pa", "Eastern Punjabi"),
    ("pcm", "Nigerian Pidgin"),
    ("pes", "Iranian Persian (Farsi)"),
    ("pl", "Polish"),
    ("pnb", "Western Punjabi"),
    ("pt", "Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("so", "Somali"),
    ("sq", "Albanian"),
    ("sv", "Swedish"),
    ("sw", "Swahili"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
    ("th", "Thai"),
    ("tl", "Tagalog"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("und", "Unknown / indeterminate"),
    ("ur", "Urdu"),
    ("vi", "Vietnamese"),
    ("wuu", "Wu Chinese"),
    ("yo", "Yoruba"),
    ("yue", "Yue Chinese (Cantonese)"),
)


def backfill_language(apps, schema_editor):
    """Keep conflicting legacy claims explicit as unknown and preserve tag data."""
    Document = apps.get_model("documents", "Document")
    Tag = apps.get_model("documents", "Tag")
    using = schema_editor.connection.alias
    codes = {code for code, _ in LANGUAGE_CHOICES}
    aliases = {"ar": "arb", "fa": "pes", "zh-cn": "cmn", "zh-tw": "cmn"}
    tag_codes = {}
    for tag_id, name in (
        Tag.objects.using(using)
        .filter(name__startswith="language:")
        .values_list("pk", "name")
    ):
        code = name.removeprefix("language:")
        code = aliases.get(code, code)
        if code in codes:
            tag_codes[tag_id] = code
    relations = (
        Document.tags.through.objects.using(using)
        .filter(tag_id__in=tag_codes)
        .order_by("document_id")
        .values_list("document_id", "tag_id")
    )
    current_id = None
    languages = set()
    for document_id, tag_id in relations.iterator(chunk_size=2000):
        if current_id is not None and current_id != document_id:
            Document._base_manager.using(using).filter(pk=current_id).update(
                language=next(iter(languages)) if len(languages) == 1 else "und"
            )
            languages.clear()
        current_id = document_id
        languages.add(tag_codes[tag_id])
    if current_id is not None:
        Document._base_manager.using(using).filter(pk=current_id).update(
            language=next(iter(languages)) if len(languages) == 1 else "und"
        )


class Migration(migrations.Migration):
    dependencies = [("documents", "0032_provider_task_types")]
    operations = [
        migrations.AddField(
            model_name="document",
            name="language",
            field=models.CharField(
                blank=True,
                choices=LANGUAGE_CHOICES,
                db_index=True,
                default="",
                max_length=3,
                help_text="Empty means unset; und means unknown or indeterminate.",
                verbose_name="language",
            ),
        ),
        migrations.AlterField(
            model_name="savedviewfilterrule",
            name="rule_type",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (0, "title contains"),
                    (1, "content contains"),
                    (2, "ASN is"),
                    (3, "correspondent is"),
                    (4, "document type is"),
                    (5, "is in inbox"),
                    (6, "has tag"),
                    (7, "has any tag"),
                    (8, "created before"),
                    (9, "created after"),
                    (10, "created year is"),
                    (11, "created month is"),
                    (12, "created day is"),
                    (13, "added before"),
                    (14, "added after"),
                    (15, "modified before"),
                    (16, "modified after"),
                    (17, "does not have tag"),
                    (18, "does not have ASN"),
                    (19, "title or content contains"),
                    (20, "fulltext query"),
                    (21, "more like this"),
                    (22, "has tags in"),
                    (23, "ASN greater than"),
                    (24, "ASN less than"),
                    (25, "storage path is"),
                    (26, "has correspondent in"),
                    (27, "does not have correspondent in"),
                    (28, "has document type in"),
                    (29, "does not have document type in"),
                    (30, "has storage path in"),
                    (31, "does not have storage path in"),
                    (32, "owner is"),
                    (33, "has owner in"),
                    (34, "does not have owner"),
                    (35, "does not have owner in"),
                    (36, "has custom field value"),
                    (37, "is shared by me"),
                    (38, "has custom fields"),
                    (39, "has custom field in"),
                    (40, "does not have custom field in"),
                    (41, "does not have custom field"),
                    (42, "custom fields query"),
                    (43, "created to"),
                    (44, "created from"),
                    (45, "added to"),
                    (46, "added from"),
                    (47, "mime type is"),
                    (48, "simple title search"),
                    (49, "simple text search"),
                    (50, "has duplicates"),
                    (51, "language is"),
                    (52, "language is in"),
                ],
                verbose_name="rule type",
            ),
        ),
        migrations.RunPython(backfill_language, migrations.RunPython.noop),
    ]

"""Retire language tags and their rule references after native publication."""

from importlib import import_module
from itertools import groupby
from operator import itemgetter

from django.apps.registry import Apps
from django.db import migrations
from django.db import models


def migrate_and_remove_language_tags(apps, schema_editor):
    """Preserve native values and unrelated tags while retiring legacy relations."""
    Document = apps.get_model("documents", "Document")
    Tag = apps.get_model("documents", "Tag")
    Rule = apps.get_model("documents", "SavedViewFilterRule")
    using = schema_editor.connection.alias
    choices = import_module(
        "documents.migrations.0033_document_language"
    ).LANGUAGE_CHOICES
    codes = {code for code, _ in choices}
    aliases = {"ar": "arb", "fa": "pes", "zh-cn": "cmn", "zh-tw": "cmn"}
    tag_codes = {}
    for tag_id, name in (
        Tag.objects.using(using)
        .filter(name__startswith="language:")
        .values_list("pk", "name")
    ):
        if not name.startswith("language:"):
            continue
        code = name.removeprefix("language:")
        code = aliases.get(code, code)
        tag_codes[tag_id] = code if code in codes else "und"
    if not tag_codes:
        return

    relations = (
        Document.tags.through.objects.using(using)
        .filter(tag_id__in=tag_codes)
        .order_by("document_id")
        .values_list("document_id", "tag_id")
    )
    for document_id, rows in groupby(
        relations.iterator(chunk_size=2000), key=itemgetter(0)
    ):
        languages = {tag_codes[tag_id] for _, tag_id in rows}
        Document._base_manager.using(using).filter(pk=document_id, language="").update(
            language=next(iter(languages)) if len(languages) == 1 else "und",
        )

    # Mixed OR and negative tag rules lose their retired references by request.
    retired = {str(tag_id): code for tag_id, code in tag_codes.items()}
    rules = (
        Rule.objects.using(using)
        .filter(rule_type__in=[6, 17, 22])
        .order_by("saved_view_id", "rule_type", "pk")
    )
    for (view_id, rule_type), rows in groupby(
        rules.iterator(), key=lambda rule: (rule.saved_view_id, rule.rule_type)
    ):
        rows = list(rows)
        values = {rule.pk: (rule.value or "").split(",") for rule in rows}
        language_values = {
            retired[value]
            for row in values.values()
            for value in row
            if value in retired
        }
        if not language_values:
            continue
        remaining = {
            rule.pk: [value for value in values[rule.pk] if value not in retired]
            for rule in rows
        }
        for rule in rows:
            if values[rule.pk] == remaining[rule.pk]:
                continue
            if remaining[rule.pk]:
                Rule.objects.using(using).filter(pk=rule.pk).update(
                    value=",".join(remaining[rule.pk])
                )
            else:
                Rule.objects.using(using).filter(pk=rule.pk).delete()
        if (
            Rule.objects.using(using)
            .filter(saved_view_id=view_id, rule_type__in=[51, 52])
            .exists()
        ):
            continue
        if rule_type == 6:
            Rule.objects.using(using).create(
                saved_view_id=view_id,
                rule_type=51,
                value=next(iter(language_values))
                if len(language_values) == 1
                else "und",
            )
        elif rule_type == 22 and not any(remaining.values()):
            Rule.objects.using(using).bulk_create(
                [
                    Rule(saved_view_id=view_id, rule_type=52, value=code)
                    for code in sorted(language_values)
                ]
            )

    # Reparent children first: tn_parent uses CASCADE on deletion.
    parents = dict(Tag.objects.using(using).values_list("pk", "tn_parent_id"))
    for child in (
        Tag.objects.using(using)
        .filter(tn_parent_id__in=tag_codes)
        .exclude(pk__in=tag_codes)
    ):
        parent_id = child.tn_parent_id
        while parent_id in tag_codes:
            parent_id = parents[parent_id]
        Tag.objects.using(using).filter(pk=child.pk).update(tn_parent_id=parent_id)
    Tag.objects.using(using).filter(pk__in=tag_codes).delete()

    # Reuse treenode's cache rebuild against the historical table, in a private registry.
    from treenode.models import TreeNodeModel

    class TagTree(TreeNodeModel):
        name = models.CharField(max_length=128)
        treenode_display_field = "name"
        objects = models.Manager().db_manager(using)

        class Meta:
            app_label = "documents"
            apps = Apps()
            managed = False
            db_table = Tag._meta.db_table

    _, dirty_instances, dirty_fields = TagTree._TreeNodeModel__get_nodes_data()
    if dirty_instances:
        TagTree.objects.bulk_update(
            dirty_instances, fields=sorted(dirty_fields), batch_size=500
        )

    # The cache is keyed by the runtime model class, independently of historical fields.
    from treenode.cache import clear_cache

    from documents.models import Tag as CurrentTag

    clear_cache(CurrentTag)


class Migration(migrations.Migration):
    dependencies = [("documents", "0033_document_language")]
    operations = [migrations.RunPython(migrate_and_remove_language_tags)]

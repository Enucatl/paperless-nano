"""Legacy language tags migrate once without deleting unrelated metadata."""

import importlib
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from documents.models import Document
from documents.models import SavedView
from documents.models import SavedViewFilterRule
from documents.models import Tag
from documents.models import WorkflowAction
from documents.models import WorkflowTrigger
from paperless_mail.models import MailAccount
from paperless_mail.models import MailRule
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("paperless_dirs")]


@pytest.fixture
def migrate_languages():
    """Run the cleanup against the current database using the migration entrypoint."""
    migration = importlib.import_module(
        "documents.migrations.0034_remove_language_tags"
    )
    historical_apps = (
        MigrationExecutor(connection)
        .loader.project_state(
            [("documents", "0033_document_language")],
        )
        .apps
    )
    return lambda: migration.migrate_and_remove_language_tags(
        historical_apps,
        SimpleNamespace(connection=connection),
    )


@pytest.mark.parametrize(
    ("codes", "existing", "expected"),
    [
        ([], "", ""),
        (["de"], "", "de"),
        (["ar"], "", "arb"),
        (["fa"], "", "pes"),
        (["zh-cn", "zh-tw"], "", "cmn"),
        (["de", "en"], "", "und"),
        (["unsupported"], "", "und"),
        (["de", "unsupported"], "", "und"),
        (["de"], "fr", "fr"),
        (["de"], "und", "und"),
    ],
)
def test_cleanup_migrates_unset_values_and_preserves_native_values(
    migrate_languages,
    codes,
    existing,
    expected,
):
    """Migrate known, conflicting and invalid values while preserving manual edits."""
    document = DocumentFactory(language=existing)
    unrelated = TagFactory(name="important")
    document.tags.add(unrelated)
    for code in codes:
        document.tags.add(TagFactory(name=f"language:{code}"))

    migrate_languages()

    document.refresh_from_db()
    assert document.language == expected
    assert list(document.tags.values_list("pk", flat=True)) == [unrelated.pk]
    assert not Tag.objects.filter(name__startswith="language:").exists()
    migrate_languages()
    document.refresh_from_db()
    assert document.language == expected


def test_cleanup_includes_trashed_documents(migrate_languages):
    """Removing tags must not lose the only language evidence of a trashed document."""
    document = DocumentFactory(language="")
    document.tags.add(TagFactory(name="language:de"))
    document.delete()

    migrate_languages()

    assert Document.global_objects.get(pk=document.pk).language == "de"
    assert not Tag.objects.filter(name__startswith="language:").exists()


def test_cleanup_migrates_saved_language_filters_and_removes_old_references(
    migrate_languages,
):
    """Keep supported native filters and unrelated portions of mixed tag filters."""
    german = TagFactory(name="language:de")
    english = TagFactory(name="language:en")
    unrelated = TagFactory(name="important")
    exact_view = SavedView.objects.create(name="German")
    SavedViewFilterRule.objects.create(
        saved_view=exact_view, rule_type=6, value=str(german.pk)
    )
    SavedViewFilterRule.objects.create(
        saved_view=exact_view, rule_type=6, value=str(unrelated.pk)
    )
    any_view = SavedView.objects.create(name="English or German")
    for tag in [german, english]:
        SavedViewFilterRule.objects.create(
            saved_view=any_view, rule_type=22, value=str(tag.pk)
        )
    mixed_view = SavedView.objects.create(name="Mixed")
    for tag in [german, unrelated]:
        SavedViewFilterRule.objects.create(
            saved_view=mixed_view, rule_type=22, value=str(tag.pk)
        )
    SavedViewFilterRule.objects.create(
        saved_view=mixed_view, rule_type=17, value=str(english.pk)
    )
    SavedViewFilterRule.objects.create(
        saved_view=mixed_view, rule_type=17, value=str(unrelated.pk)
    )
    SavedViewFilterRule.objects.create(
        saved_view=mixed_view, rule_type=0, value="invoice"
    )

    migrate_languages()

    assert set(exact_view.filter_rules.values_list("rule_type", "value")) == {
        (51, "de"),
        (6, str(unrelated.pk)),
    }
    assert set(any_view.filter_rules.values_list("rule_type", "value")) == {
        (52, "de"),
        (52, "en"),
    }
    assert set(mixed_view.filter_rules.values_list("rule_type", "value")) == {
        (22, str(unrelated.pk)),
        (17, str(unrelated.pk)),
        (0, "invoice"),
    }


def test_cleanup_preserves_descendants_and_rebuilds_tree_metadata(migrate_languages):
    """Reparent surviving tags past removed ancestors without cascading their deletion."""
    ancestor = TagFactory(name="unrelated root")
    german = TagFactory(name="language:de", tn_parent=ancestor)
    english = TagFactory(name="language:en", tn_parent=german)
    child = TagFactory(name="important", tn_parent=english)
    removed_root = TagFactory(name="language:unsupported")
    orphan = TagFactory(name="other root", tn_parent=removed_root)

    migrate_languages()

    ancestor.refresh_from_db()
    child.refresh_from_db()
    orphan.refresh_from_db()
    assert child.tn_parent_id == ancestor.pk
    assert orphan.tn_parent_id is None
    assert ancestor.tn_children_count == 1
    assert ancestor.tn_descendants_count == 1
    assert child.tn_ancestors_count == 1
    assert child.tn_level == 2
    assert orphan.tn_ancestors_count == 0
    assert orphan.tn_level == 1
    assert set(Tag.objects.values_list("pk", flat=True)) == {
        ancestor.pk,
        child.pk,
        orphan.pk,
    }


def test_cleanup_removes_workflow_and_mail_tag_references(migrate_languages):
    """Remove retired tag references while preserving rules and unrelated assignments."""
    language = TagFactory(name="language:de")
    unrelated = TagFactory(name="important")
    trigger = WorkflowTrigger.objects.create()
    action = WorkflowAction.objects.create()
    account = MailAccount.objects.create(
        name="mail", imap_server="example.com", username="test", password="test"
    )
    mail_rule = MailRule.objects.create(name="attachments", account=account)
    relations = [
        trigger.filter_has_tags,
        trigger.filter_has_all_tags,
        trigger.filter_has_not_tags,
        action.assign_tags,
        action.remove_tags,
        mail_rule.assign_tags,
    ]
    for relation in relations:
        relation.add(language, unrelated)

    migrate_languages()

    assert WorkflowTrigger.objects.filter(pk=trigger.pk).exists()
    assert WorkflowAction.objects.filter(pk=action.pk).exists()
    assert MailRule.objects.filter(pk=mail_rule.pk).exists()
    for relation in relations:
        assert list(relation.values_list("pk", flat=True)) == [unrelated.pk]


def test_cleanup_preserves_existing_native_saved_language_filter(migrate_languages):
    """A native saved filter is authoritative when a retired tag rule also exists."""
    german = TagFactory(name="language:de")
    unrelated = TagFactory(name="important")
    view = SavedView.objects.create(name="Native English")
    SavedViewFilterRule.objects.create(saved_view=view, rule_type=51, value="en")
    SavedViewFilterRule.objects.create(
        saved_view=view, rule_type=6, value=str(german.pk)
    )
    SavedViewFilterRule.objects.create(
        saved_view=view, rule_type=6, value=str(unrelated.pk)
    )

    migrate_languages()

    assert set(view.filter_rules.values_list("rule_type", "value")) == {
        (51, "en"),
        (6, str(unrelated.pk)),
    }


def test_cleanup_invalidates_live_tag_tree_cache(migrate_languages):
    """The live hierarchy cache must not retain deleted tags or old parent links."""
    from treenode.cache import query_cache

    language = TagFactory(name="language:de")
    child = TagFactory(name="important", tn_parent=language)
    assert {tag.pk for tag in query_cache(Tag)} == {language.pk, child.pk}

    migrate_languages()

    cached = query_cache(Tag)
    assert [tag.pk for tag in cached] == [child.pk]
    assert cached[0].tn_parent_id is None
    assert cached[0].tn_ancestors_count == 0

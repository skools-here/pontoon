import csv
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.template.defaultfilters import slugify
from django.utils.datastructures import MultiValueDictKeyError

from pontoon.administration.forms import (
    EntityFormSet,
    ExternalResourceInlineFormSet,
    ProjectForm,
    RepositoryInlineFormSet,
    TagInlineFormSet,
)
from pontoon.base import utils
from pontoon.base.models import (
    Entity,
    Locale,
    Project,
    ProjectLocale,
    Resource,
    TranslatedResource,
    Translation,
)
from pontoon.base.utils import require_AJAX
from pontoon.pretranslation.tasks import pretranslate_task
from pontoon.sync.tasks import sync_project_task


log = logging.getLogger(__name__)


def admin(request):
    """Admin interface."""
    if not request.user.has_perm("base.can_manage_project"):
        raise PermissionDenied

    projects = Project.objects.prefetch_related(
        "latest_translation__user", "latest_translation__approved_user"
    ).order_by("name")

    enabled_projects = projects.filter(disabled=False)
    disabled_projects = projects.filter(disabled=True)
    project_stats = projects.stats_data_as_dict()

    return render(
        request,
        "admin.html",
        {
            "admin": True,
            "enabled_projects": enabled_projects,
            "disabled_projects": disabled_projects,
            "project_stats": project_stats,
        },
    )


@login_required(redirect_field_name="", login_url="/403")
@require_AJAX
def get_slug(request):
    """Convert project name to slug."""
    if not request.user.has_perm("base.can_manage_project"):
        return JsonResponse(
            {
                "status": False,
                "message": "Forbidden: You don't have permission to retrieve the project slug.",
            },
            status=403,
        )

    try:
        name = request.GET["name"]
    except MultiValueDictKeyError as e:
        return JsonResponse(
            {"status": False, "message": f"Bad Request: {e}"},
            status=400,
        )

    slug = slugify(name)
    return HttpResponse(slug)


@login_required(redirect_field_name="", login_url="/403")
@require_AJAX
def get_project_locales(request):
    """Get a map of project names and corresponding locale codes."""
    if not request.user.has_perm("base.can_manage_project"):
        return JsonResponse(
            {
                "status": False,
                "message": "Forbidden: You don't have permission to retrieve project locales.",
            },
            status=403,
        )

    data = {}
    for p in Project.objects.prefetch_related("locales"):
        data[p.name] = [locale.pk for locale in p.locales.all()]

    return JsonResponse(data, safe=False)


@transaction.atomic
def manage_project(request, slug=None, template="admin_project.html"):
    """Admin project."""
    log.debug("Admin project.")

    if not request.user.has_perm("base.can_manage_project"):
        raise PermissionDenied

    form = ProjectForm()
    repo_formset = RepositoryInlineFormSet()
    external_resource_formset = ExternalResourceInlineFormSet()
    tag_formset = TagInlineFormSet()
    locales_readonly = Locale.objects.none()
    locales_selected = Locale.objects.none()
    locales_pretranslate = Locale.objects.none()
    subtitle = "Add project"
    pk = None
    project = None

    # Save project
    if request.method == "POST":
        locales_readonly = Locale.objects.filter(
            pk__in=request.POST.getlist("locales_readonly")
        )
        locales_selected = Locale.objects.filter(
            pk__in=request.POST.getlist("locales")
        ).exclude(pk__in=locales_readonly)
        locales_pretranslate = Locale.objects.filter(
            pk__in=request.POST.getlist("locales_pretranslate")
        )

        # Update existing project
        try:
            pk = request.POST["pk"]
            project = Project.objects.visible_for(request.user).get(pk=pk)
            form = ProjectForm(request.POST, instance=project)
            # Needed if form invalid
            repo_formset = RepositoryInlineFormSet(request.POST, instance=project)
            tag_formset = (
                TagInlineFormSet(request.POST, instance=project)
                if project.tags_enabled
                else None
            )
            external_resource_formset = ExternalResourceInlineFormSet(
                request.POST, instance=project
            )
            subtitle = "Edit project"

        # Add a new project
        except MultiValueDictKeyError:
            form = ProjectForm(request.POST)
            # Needed if form invalid
            repo_formset = RepositoryInlineFormSet(request.POST)
            external_resource_formset = ExternalResourceInlineFormSet(request.POST)
            tag_formset = None

        if form.is_valid():
            project = form.save(commit=False)
            repo_formset = RepositoryInlineFormSet(request.POST, instance=project)
            external_resource_formset = ExternalResourceInlineFormSet(
                request.POST, instance=project
            )
            if tag_formset:
                tag_formset = TagInlineFormSet(request.POST, instance=project)
            formsets_valid = (
                repo_formset.is_valid()
                and external_resource_formset.is_valid()
                and (tag_formset.is_valid() if tag_formset else True)
            )
            if formsets_valid:
                project.save()

                # Manually save ProjectLocales due to intermediary model
                locales_form = form.cleaned_data.get("locales", [])
                locales_readonly_form = form.cleaned_data.get("locales_readonly", [])
                locales = locales_form | locales_readonly_form

                (
                    ProjectLocale.objects.filter(project=project)
                    .exclude(locale__pk__in=[loc.pk for loc in locales])
                    .delete()
                )

                for locale in locales:
                    # The implicit pre_save and post_save signals sent here are required
                    # to maintain django-guardian permissions.
                    ProjectLocale.objects.get_or_create(project=project, locale=locale)

                project_locales = ProjectLocale.objects.filter(project=project)

                # Update readonly flags
                locales_readonly_pks = [loc.pk for loc in locales_readonly_form]
                project_locales.filter(readonly=True).exclude(
                    locale__pk__in=locales_readonly_pks
                ).update(readonly=False)
                project_locales.filter(
                    locale__pk__in=locales_readonly_pks, readonly=False
                ).update(readonly=True)

                # Update pretranslate flags
                locales_pretranslate_form = form.cleaned_data.get(
                    "locales_pretranslate", []
                )
                locales_pretranslate_pks = [loc.pk for loc in locales_pretranslate_form]
                project_locales.filter(pretranslation_enabled=True).exclude(
                    locale__pk__in=locales_pretranslate_pks,
                ).update(pretranslation_enabled=False)
                project_locales.filter(
                    locale__pk__in=locales_pretranslate_pks,
                    pretranslation_enabled=False,
                ).update(pretranslation_enabled=True)

                repo_formset.save()
                external_resource_formset.save()
                if tag_formset:
                    tag_formset.save()

                # If the data source is database and there are new strings, save them.
                if project.data_source == Project.DataSource.DATABASE:
                    _save_new_strings(project, request.POST.get("new_strings", ""))
                    _create_or_update_translated_resources(project, locales)

                # Properly displays formsets, but removes errors (if valid only)
                repo_formset = RepositoryInlineFormSet(instance=project)
                external_resource_formset = ExternalResourceInlineFormSet(
                    instance=project
                )
                if project.tags_enabled:
                    tag_formset = TagInlineFormSet(instance=project)
                subtitle += ". Saved."
                pk = project.pk
            else:
                subtitle += ". Error."
        else:
            subtitle += ". Error."

    # If URL specified and found, show edit, otherwise show add form
    elif slug is not None:
        try:
            project = Project.objects.get(slug=slug)
            pk = project.pk
            form = ProjectForm(instance=project)
            repo_formset = RepositoryInlineFormSet(instance=project)
            tag_formset = (
                TagInlineFormSet(instance=project) if project.tags_enabled else None
            )
            external_resource_formset = ExternalResourceInlineFormSet(instance=project)
            locales_readonly = Locale.objects.filter(
                project_locale__readonly=True,
                project_locale__project=project,
            )
            locales_selected = project.locales.exclude(pk__in=locales_readonly)
            locales_pretranslate = Locale.objects.filter(
                project_locale__pretranslation_enabled=True,
                project_locale__project=project,
            )
            subtitle = "Edit project"
        except Project.DoesNotExist:
            form = ProjectForm(initial={"slug": slug})

    # Override default label suffix
    form.label_suffix = ""

    projects = sorted([p.name for p in Project.objects.all()])

    locales_available = Locale.objects.exclude(pk__in=locales_readonly).exclude(
        pk__in=locales_selected
    )

    locales_pretranslate_available = locales_selected.exclude(
        pk__in=locales_pretranslate
    )

    # Admins reason in terms of locale codes (see bug 1394194)
    locales_readonly = locales_readonly.order_by("code")
    locales_selected = locales_selected.order_by("code")
    locales_available = locales_available.order_by("code")
    locales_pretranslate = locales_pretranslate.order_by("code")
    locales_pretranslate_available = locales_pretranslate_available.order_by("code")

    data = {
        "slug": slug,
        "form": form,
        "repo_formset": repo_formset,
        "tag_formset": tag_formset,
        "external_resource_formset": external_resource_formset,
        "locales_readonly": locales_readonly,
        "locales_selected": locales_selected,
        "locales_available": locales_available,
        "locales_pretranslate": locales_pretranslate,
        "locales_pretranslate_available": locales_pretranslate_available,
        "subtitle": subtitle,
        "pk": pk,
        "project": project,
        "projects": projects,
    }

    # Set locale in Translate link
    if Resource.objects.filter(project=project).exists() and locales_selected:
        locale = (
            utils.get_project_locale_from_request(request, project.locales)
            or locales_selected[0].code
        )
        if locale:
            data["translate_locale"] = locale

    return render(request, template, data)


def _get_project_strings_csv(project, entities, output):
    """Return a CSV content of all strings and translations for a project and locale.

    The file format looks as follow:

        source, locale_code_1, locale_code_2
        "string A", "tranlation A1", "tranlation A2"
        "string B", "tranlation B1", "tranlation B2"

    The first column has all source strings. Then there is one column per enabled locale, each
    containing available translations for each source string (or an empty cell). The first line
    contains the code of each locale, expect for the first cell which is always "source".

    :arg Project project: the project from which to take strings
    :arg list entities: the list of all entities of the project
    :arg buffer output: a buffer to which the CSV writer will send its data

    :returns: the same output object with the CSV data

    """
    locales = Locale.objects.filter(project_locale__project=project)
    translations = (
        Translation.objects.filter(
            entity__resource__project=project,
            approved=True,
        )
        .prefetch_related("locale")
        .prefetch_related("entity")
    )
    all_data = {x.id: {"source": x.string} for x in entities}

    for translation in translations:
        all_data[translation.entity.id][translation.locale.code] = translation.string

    headers = ["source"] + [x.code for x in locales]
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(all_data.values())

    return output


def _get_resource_for_database_project(project):
    """Return the Resource object of an in database project.

    If the project has no resource yet, create a new one and return it.
    Otherwise, return the existing resource.

    Note that a database project should always have only one resource.

    :arg Project project: the in-database Project object

    :returns: the unique Resource object associated with the project

    """
    try:
        return Resource.objects.get(
            project=project,
        )
    except Resource.DoesNotExist:
        # There's no resource for that project yet, create one.
        resource = Resource(
            path="database",
            project=project,
        )
        resource.save()
        return resource
    except Resource.MultipleObjectsReturned:
        # There are several resources for this project, that should not
        # be allowed. Log an error and raise.
        log.error(
            "There is more than 1 Resource for in_database project %s" % project.name
        )
        raise


def _save_new_strings(project, source):
    """Save a batch of strings into an existing project.

    This function takes a batch of new strings as a blob of text, separate individual
    strings by new lines, and then stores each one as a new source string for the project.

    :arg Project project: the Project object to which new strings will be associated
    :arg string source: a text of new-line-separated source strings

    :returns: True if new strings have been saved, False otherwise

    """
    new_strings = source.strip().split("\n")

    # Remove empty strings from the list.
    new_strings = [x.strip() for x in new_strings if x.strip()]

    if new_strings:
        # Create a new fake resource for that project.
        resource = _get_resource_for_database_project(project)
        resource.total_strings = len(new_strings)
        resource.save()

        # Insert all new strings into Entity objects, associated to the fake resource.
        new_entities = []
        for index, new_string in enumerate(new_strings):
            string = new_string.strip()
            new_entities.append(Entity(string=string, resource=resource, order=index))

        Entity.objects.bulk_create(new_entities)

        return True

    return False


def _create_or_update_translated_resources(
    project,
    locales=None,
    resource=None,
):
    if locales is None:
        locales = Locale.objects.filter(project_locale__project=project)

    if resource is None:
        resource = _get_resource_for_database_project(project)

    for locale in locales:
        tr, _ = TranslatedResource.objects.get_or_create(
            locale_id=locale.pk,
            resource=resource,
        )
        tr.calculate_stats()


def manage_project_strings(request, slug=None):
    """View to manage the source strings of a project.

    This view is only accessible for projects that do not have a "Source repo". It allows
    admins to add new strings to a project in a batch, and then to edit, remove or comment on
    any strings.

    """
    if not request.user.has_perm("base.can_manage_project"):
        raise PermissionDenied

    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        raise Http404

    if project.data_source != Project.DataSource.DATABASE:
        return HttpResponseForbidden(
            "Project %s's strings come from a repository, managing strings is forbidden."
            % project.name
        )

    entities = Entity.objects.filter(resource__project=project, obsolete=False)
    project_has_strings = entities.exists()
    formset = EntityFormSet(queryset=entities)

    if request.GET.get("format") == "csv":
        # Return a CSV document containing all translations for this project.
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="%s.csv"' % project.name

        return _get_project_strings_csv(project, entities, response)

    if request.method == "POST":
        if not project_has_strings:
            # We are receiving new strings in a batch.
            new_strings_source = request.POST.get("new_strings", "")
            if _save_new_strings(project, new_strings_source):
                project_has_strings = True  # we have strings now!
                _create_or_update_translated_resources(project)
        else:
            # Get all strings, find the ones that changed, update them in the database.
            formset = EntityFormSet(request.POST, queryset=entities)
            if formset.is_valid():
                resource = Resource.objects.filter(project=project).first()
                entity_max_order = entities.aggregate(Max("order"))["order__max"]
                try:
                    # This line can purposefully cause an exception, and that
                    # causes trouble in tests, because all tests are
                    # encapsulated in a single transation. Django thus refuses
                    # to run any other requests after one has failed, until the
                    # end of the transation.
                    # Using transation.atomic here is the way to tell django
                    # that this is fine.
                    # See https://stackoverflow.com/questions/21458387/
                    with transaction.atomic():
                        formset.save()
                except IntegrityError:
                    # This happens when the user creates a new string. By default,
                    # it has no resource, and that's a violation of the database
                    # constraints. So, we want to make sure all entries have a resource.
                    new_entities = formset.save(commit=False)
                    for entity in new_entities:
                        if not entity.resource_id:
                            entity.resource = resource

                        # We also use this opportunity to give the new entity
                        # an order.
                        entity_max_order += 1
                        entity.order = entity_max_order

                        # Note that we save all entities one by one. That shouldn't be a problem
                        # because we don't expect users to change thousands of strings at once.
                        # Also, django is smart and ``formset.save()`` only returns Entity
                        # objects that have changed.
                        entity.save()

                # Update stats with the new number of strings.
                resource.total_strings = Entity.objects.filter(
                    resource=resource, obsolete=False
                ).count()
                resource.save()
                _create_or_update_translated_resources(project, resource=resource)

            # Reinitialize the formset.
            formset = EntityFormSet(queryset=entities)

    data = {
        "project": project,
        "entities": entities,
        "project_has_strings": project_has_strings,
        "entities_form": formset,
    }
    return render(request, "admin_project_strings.html", data)


@login_required(redirect_field_name="", login_url="/403")
@require_AJAX
def manually_sync_project(request, slug):
    if not request.user.has_perm("base.can_manage_project") or not settings.MANUAL_SYNC:
        return HttpResponseForbidden(
            "Forbidden: You don't have permission for syncing projects"
        )

    project = Project.objects.get(slug=slug)
    sync_project_task.delay(project.pk)

    return HttpResponse("ok")


@login_required(redirect_field_name="", login_url="/403")
@require_AJAX
def manually_pretranslate_project(request, slug):
    if not request.user.has_perm("base.can_manage_project"):
        return HttpResponseForbidden(
            "Forbidden: You don't have permission for pretranslating projects"
        )

    project = Project.objects.get(slug=slug)
    pretranslate_task.delay(project.pk)

    return HttpResponse("ok")

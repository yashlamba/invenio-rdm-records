# -*- coding: utf-8 -*-
#
# Copyright (C) 2023 CERN.
#
# Invenio-RDM-Records is free software; you can redistribute it and/or modify
# it under the terms of the MIT License; see LICENSE file for more details.

"""RDM Record Communities Service."""

from invenio_communities.proxies import current_communities
from invenio_i18n import lazy_gettext as _
from invenio_pidstore.errors import PIDDoesNotExistError
from invenio_records_resources.services import (
    RecordIndexerMixin,
    Service,
    ServiceSchemaWrapper,
)
from invenio_records_resources.services.errors import PermissionDeniedError
from invenio_records_resources.services.uow import (
    IndexRefreshOp,
    RecordCommitOp,
    RecordIndexOp,
    unit_of_work,
)
from invenio_requests import current_request_type_registry, current_requests_service
from invenio_requests.resolvers.registry import ResolverRegistry
from invenio_search.engine import dsl
from sqlalchemy.orm.exc import NoResultFound

from invenio_rdm_records.proxies import current_rdm_records
from invenio_rdm_records.requests import CommunityInclusion
from invenio_rdm_records.services.errors import (
    CommunityAlreadyExists,
    CommunityInclusionInconsistentAccessRestrictions,
    OpenRequestAlreadyExists,
    RecordCommunityMissing,
)


class RecordCommunitiesService(Service, RecordIndexerMixin):
    """Record communities service.

    The communities service is in charge of managing communities of a given record.
    """

    @property
    def schema(self):
        """Returns the data schema instance."""
        return ServiceSchemaWrapper(self, schema=self.config.schema)

    @property
    def record_cls(self):
        """Factory for creating a record class."""
        return self.config.record_cls

    def _exists(self, identity, community_id, record):
        """Return the request id if an open request already exists, else None."""
        results = current_requests_service.search(
            identity,
            extra_filter=dsl.query.Bool(
                "must",
                must=[
                    dsl.Q("term", **{"receiver.community": community_id}),
                    dsl.Q("term", **{"topic.record": record.pid.pid_value}),
                    dsl.Q("term", **{"type": CommunityInclusion.type_id}),
                    dsl.Q("term", **{"is_open": True}),
                ],
            ),
        )
        return next(results.hits)["id"] if results.total > 0 else None

    def _include(self, identity, community_id, comment, require_review, record, uow):
        """Create request to add the community to the record."""
        already_included = community_id in record.parent.communities
        if already_included:
            raise CommunityAlreadyExists()

        # check if the community exists
        community = current_communities.service.record_cls.pid.resolve(community_id)

        # check if there is already an open request, to avoid duplications
        existing_request_id = self._exists(identity, str(community.id), record)
        if existing_request_id:
            raise OpenRequestAlreadyExists(existing_request_id)

        type_ = current_request_type_registry.lookup(CommunityInclusion.type_id)
        receiver = ResolverRegistry.resolve_entity_proxy(
            {"community": community_id}
        ).resolve()

        data = {"payload": {"content": comment, "format": "html"}} if comment else {}
        request_item = current_requests_service.create(
            identity,
            data,
            type_,
            receiver,
            topic=record,
            uow=uow,
        )
        # create review request
        request_item = current_rdm_records.community_inclusion_service.submit(
            identity, record, community, request_item._request, data, uow
        )
        # include directly when allowed
        if not require_review:
            request_item = current_rdm_records.community_inclusion_service.include(
                identity, community, request_item._request, uow
            )
        return request_item

    @unit_of_work()
    def add(self, identity, id_, data, uow):
        """Include the record in the given communities."""
        valid_data, errors = self.schema.load(
            data,
            context={
                "identity": identity,
                "max_number": self.config.max_number_of_additions,
            },
            raise_errors=True,
        )
        communities = valid_data["communities"]

        record = self.record_cls.pid.resolve(id_)
        self.require_permission(identity, "add_community", record=record)

        success = []
        for community in communities:
            community_id = community["id"]
            comment = community.get("comment", "")
            require_review = community.get("require_review", False)

            result = {
                "community": community_id,
            }
            try:
                request_item = self._include(
                    identity, community_id, comment, require_review, record, uow
                )
                result["request"] = str(request_item.data["id"])
                success.append(result)
            except (NoResultFound, PIDDoesNotExistError):
                result["message"] = _("Community not found.")
                errors.append(result)
            except CommunityAlreadyExists:
                result["message"] = _(
                    "The record is already included in this community."
                )
                errors.append(result)
            except OpenRequestAlreadyExists:
                result["message"] = _(
                    "There is already an open inclusion request for this community."
                )
                errors.append(result)
            except CommunityInclusionInconsistentAccessRestrictions as ex:
                result["message"] = ex.args[0]
                errors.append(result)
            except PermissionDeniedError:
                result["message"] = _("Permission denied.")
                errors.append(result)

        uow.register(IndexRefreshOp(indexer=self.indexer))

        return dict(success=success, errors=errors)

    def _remove(self, identity, community_id, record):
        """Remove a community from the record."""
        if community_id not in record.parent.communities.ids:
            raise RecordCommunityMissing(record.id, community_id)

        # check permission here, per community: curator cannot remove another community
        self.require_permission(
            identity, "remove_community", record=record, community_id=community_id
        )

        # Default community is deleted when the exact same community is removed from the record
        record.parent.communities.remove(community_id)

    @unit_of_work()
    def remove(self, identity, id_, data, uow):
        """Remove communities from the record."""
        record = self.record_cls.pid.resolve(id_)

        valid_data, errors = self.schema.load(
            data,
            context={
                "identity": identity,
                "max_number": self.config.max_number_of_removals,
            },
            raise_errors=True,
        )
        communities = valid_data["communities"]
        commit_changes = False
        for community in communities:
            community_id = community["id"]
            try:
                self._remove(identity, community_id, record)
                commit_changes = True
            except RecordCommunityMissing:
                errors.append(
                    {
                        "community": community_id,
                        "message": _("The record does not belong to the community."),
                    }
                )
            except PermissionDeniedError:
                errors.append(
                    {
                        "community": community_id,
                        "message": _("Permission denied."),
                    }
                )
        if commit_changes:
            uow.register(RecordCommitOp(record.parent))
            uow.register(
                RecordIndexOp(record, indexer=self.indexer, index_refresh=True)
            )

        return errors

    def search(
        self,
        identity,
        id_,
        params=None,
        search_preference=None,
        expand=False,
        extra_filter=None,
        **kwargs
    ):
        """Search for record's communities."""
        record = self.record_cls.pid.resolve(id_)
        self.require_permission(identity, "read", record=record)

        communities_ids = record.parent.communities.ids
        communities_filter = dsl.Q("terms", **{"id": [id_ for id_ in communities_ids]})
        if extra_filter is not None:
            communities_filter = communities_filter & extra_filter

        return current_communities.service.search(
            identity,
            params=params,
            search_preference=search_preference,
            expand=expand,
            extra_filter=communities_filter,
            **kwargs
        )

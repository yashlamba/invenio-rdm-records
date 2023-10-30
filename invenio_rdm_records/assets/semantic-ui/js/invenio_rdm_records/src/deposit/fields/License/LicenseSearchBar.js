// This file is part of Invenio-RDM-Records
// Copyright (C) 2020-2023 CERN.
// Copyright (C) 2020-2022 Northwestern University.
//
// Invenio-RDM-Records is free software; you can redistribute it and/or modify it
// under the terms of the MIT License; see LICENSE file for more details.

import React, { useContext } from "react";
import { SearchBar as SKSearchBar, Sort } from "react-searchkit";
import { i18next } from "@translations/invenio_administration/i18next";

export const LicenseSearchBar = (props) => {
  return (
    <SKSearchBar
      placeholder={i18next.t("Search")}
      autofocus
      actionProps={{
        icon: "search",
        content: null,
        className: "search",
      }}
    />
  );
};

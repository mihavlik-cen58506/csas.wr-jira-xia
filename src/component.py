"""
XIA Nightly Processor - Keboola custom component entrypoint.

Revalidates queued Jira issues for pending XIA archive requests, bulk-moves eligible
issues into the Jira XIA project, and persists outcomes back to the XIA_REQUESTS,
XIA_REQUEST_ITEMS and XIA_RUNS tables (roles resolved from Input/Output Mapping by name).
"""

import logging

from keboola.component.base import ComponentBase
from keboola.component.exceptions import UserException

import csv_io
from configuration import Configuration
from constants import TABLE_ROLE_REQUEST_ITEMS, TABLE_ROLE_REQUESTS, TABLE_ROLE_RUNS
from jira_api import JiraApiClient
from processor import process_pending_requests


class Component(ComponentBase):
    """Extends base class for general Python components. Initializes the CommonInterface
    and performs configuration validation.
    """

    def __init__(self):
        super().__init__()

    def run(self):
        """Main execution code."""
        params = Configuration(**self.configuration.parameters)

        input_tables = self.get_input_tables_definitions()
        if len(input_tables) == 0:
            raise UserException("No input tables found. Expected XIA_REQUESTS, XIA_REQUEST_ITEMS and XIA_RUNS.")

        requests_in = csv_io.resolve_table_by_role(input_tables, TABLE_ROLE_REQUESTS)
        items_in = csv_io.resolve_table_by_role(input_tables, TABLE_ROLE_REQUEST_ITEMS)
        runs_in = csv_io.resolve_table_by_role(input_tables, TABLE_ROLE_RUNS)

        requests_rows, requests_fields = csv_io.read_table(requests_in)
        items_rows, items_fields = csv_io.read_table(items_in)
        runs_rows, runs_fields = csv_io.read_table(runs_in)

        csv_io.validate_required_columns(requests_fields, TABLE_ROLE_REQUESTS)
        csv_io.validate_required_columns(items_fields, TABLE_ROLE_REQUEST_ITEMS)
        csv_io.validate_required_columns(runs_fields, TABLE_ROLE_RUNS)

        jira = JiraApiClient(params.jira_base_url, params.jira_username, params.jira_api_token)

        logging.info("XIA Nightly - starting.")
        process_pending_requests(jira, requests_rows, items_rows, runs_rows)
        logging.info("XIA Nightly - finished.")

        requests_out = self.create_out_table_definition(requests_in.name, incremental=False)
        items_out = self.create_out_table_definition(items_in.name, incremental=False)
        runs_out = self.create_out_table_definition(runs_in.name, incremental=False)

        csv_io.write_table(requests_out, requests_rows, requests_fields)
        csv_io.write_table(items_out, items_rows, items_fields)
        csv_io.write_table(runs_out, runs_rows, runs_fields)

        self.write_manifest(requests_out)
        self.write_manifest(items_out)
        self.write_manifest(runs_out)


"""
        Main entrypoint
"""
if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)

import sys
import pytz
import time
import uuid
import json
import logging
import configargparse
from configargparse import RawTextHelpFormatter
from icalendar import Calendar
import todoist


# TODO: Apple reminders doesn't export location-based reminder data. Need to manually handle those after import.


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.FileHandler('todoist_importer.log')
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


class TodoistAPI():


    def __init__(self, api_token, do_commit):
        self.api_token = api_token
        self.api = todoist.TodoistAPI(self.api_token)
        self.api.sync(resource_types=['all'])
        self._command_count = 0
        self._do_commit = do_commit


    def _chunk_api(func):
        def wrapper(self, *args, **kwargs):
            self._command_count += 1

            # The Todoist API has a command limit of 100 per commit. Leaving some wiggle room.
            if self._command_count > 90:
                logger.debug("API call chunk size reached. Committing chunk.")
                self.commit()
                self._command_count = 0
            return func(self, *args, **kwargs)
        return wrapper


    def get_project(self, project_name):
        for project in self.api.projects.all():
            if project['name'] == project_name:
                return project
        return self._create_project(project_name)


    @_chunk_api
    def _create_project(self, project_name):
        temp_id = str(uuid.uuid4())
        project = self.api.projects.add(temp_id=temp_id, name=project_name)
        return project


    @_chunk_api
    def add_item(self, project, ical_item):
        td_item_info = {}
        td_item_info['content'] = ical_item['SUMMARY'].encode('utf-8')
        try:
            td_item_info['due_date_utc'] = ical_item['DUE'].dt.astimezone(pytz.timezone('UTC')).strftime('%Y-%m-%dT%H:%M')
            td_item_info['date_string'] = td_item_info['due_date_utc']
        except KeyError:
            logger.debug("Item has no associated date: %s" % ical_item['SUMMARY'])

        try:
            td_item_info['status'] = ical_item['STATUS']
        except KeyError:
            pass

        td_item_info['has_notifications'] = True

        allowed_recurrence_keys = ['FREQ', 'BYDAY']
        try:
            rrule = ical_item['RRULE']
            for key in rrule.keys():
                if key not in allowed_recurrence_keys:
                    logger.exception("Item recurrence not recognized: %s" % ical_item['SUMMARY'])
                date_string = rrule['FREQ'][0]
                td_item_info['date_string'] = date_string
        except KeyError:
            logger.debug("Item is not recurring: '%s'. " % ical_item['SUMMARY'])

        logger.debug("iCal task info to be added: %s" % td_item_info)
        item = self.api.items.add(project_id=project['id'], **td_item_info)
        logger.debug("Added item %s" % item.data)

        # Mark the item as completed if necessary
        self.close_item(ical_item, item)

        return item


    @_chunk_api
    def close_item(self, ical_item, td_item):
        try:
            if ical_item['STATUS'] == 'COMPLETED':
                td_item.close()
                logger.info("Marked item %s as completed" % td_item['id'])
        except KeyError:
            logger.debug("Item %s is not completed" % td_item['id'])

        return td_item


    @_chunk_api
    def add_reminder(self, item):
        try:
            reminder = self.api.reminders.add(item_id=item['id'], service="push", type='relative', minute_offset=0)
            logger.debug("Added reminder: %s " % reminder.data)
        except KeyError:
            logger.debug("Not adding reminder. Item %s has no associated date: %s" % item['id'])

        return reminder


    def commit(self):
        if self._do_commit == True:
            MAX_TRIES = 3
            tries = 0
            while True:
                response = self.api.commit()
                logger.info("Commit response: %s" % response)
                if isinstance(response, dict):
                    if response.has_key('error_code') and tries < MAX_TRIES:
                        # Todoist has an API rate limit of 50 requests/min. error_code 35 means we hit the limit.
                        if response['error_code'] == 35:
                            logger.info("API rate limit reached. Waiting 65 seconds before trying again.")
                            time.sleep(65)
                            tries += 1
                            continue
                        # Error code 40 means the API is unavailable. There should be a retry_after we can check
                        if response['error_code'] == 40:
                            retry_after = 30 # retry after 30s by default
                            try:
                                error_extra = response['error_extra']
                                retry_after = error_extra['retry_after']
                            except:
                                pass
                            logger.info("API unavailable. Retry after %d seconds." % retry_after)
                            continue

                break
            if tries < MAX_TRIES:
                logger.info("Commit succeeded")
            else:
                logger.exception("Commit failed - too many retries. Aborting.")
                sys.exit("Too many retries")
        else:
            logger.info("No-op is set. Not committing.")


def main(argv):

    parser = configargparse.ArgumentParser(description="iCal task importer for Todoist", formatter_class=RawTextHelpFormatter)
    parser.add_argument( '-f', '--filename', required=True, type=str, help='iCal file to import' )
    parser.add_argument( '-t', '--api_token', required=True, type=str, help='Todoist API token' )
    parser.add_argument( '-p', '--project', required=True, type=str, help='Destination Todoist project for these tasks' )
    parser.add_argument( '-r', '--reminders', help='Add reminders to tasks that have a due date specified\nREQUIRES TODOIST PREMIUM\nDefault: False', action='store_true')
    parser.add_argument( '--debug', help='Run in debug mode (prints more information to the logfile)\nDefault: False', action='store_true')
    parser.add_argument( '--noop', help='Parse the iCal data but don\'t actually commit to the Todoist API\nDefault: False', action='store_true')
    args=parser.parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)

    cal = Calendar.from_ical(
        open(args.filename, 'rb').read()
    )

    tdapi = TodoistAPI(args.api_token, args.noop==False)
    project = tdapi.get_project(args.project)
    logger.debug('Using project %s' % project.data)
    new_items = []
    for ical_item in cal.walk('vtodo'):
        item = tdapi.add_item(project, ical_item)
        new_items.append(item)
    tdapi.commit()


    if args.reminders:
        for item in new_items:
            tdapi.add_reminder(item)
    tdapi.commit()

if __name__ == "__main__":
    main(sys.argv[1:])

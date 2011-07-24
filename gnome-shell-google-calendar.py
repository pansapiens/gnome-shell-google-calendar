#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from time import mktime, sleep
from getpass import getpass
from threading import Thread

import gtk
import dbus
import dbus.service
import dbus.mainloop.glib
from gdata.calendar.service import CalendarService, CalendarEventQuery
import iso8601

import keyring


def calendar_month_range(date, first_day_of_week=7):
    """Returns range of dates displayed on calendars for `date`'s month.
    Parameters:
     - `date`: definies month which's range to return
     - `first_day_of_week`: integer representing first day of week used by
                            calendar; Monday -> 1, ..., Sunday -> 7
    """
    start_date = date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while start_date.isocalendar()[2] != first_day_of_week:
        start_date -= timedelta(days=1)

    end_date = date.replace(day=28, hour=23, minute=59, second=59,
            microsecond=999999)
    initial_month = end_date.month
    while end_date.month == initial_month:
        end_date += timedelta(days=1)
    end_date -= timedelta(days=1)
    last_day_of_week = first_day_of_week - 1 or 7
    while end_date.isocalendar()[2] != last_day_of_week:
        end_date += timedelta(days=1)

    return int(mktime(start_date.timetuple())),\
            int(mktime(end_date.timetuple()))


class MonthEvents(object):
    def __init__(self, key, events):
        self.start = key[0]
        self.end = key[1]
        self.events = events
        self.last_update = datetime.now()

    def add_event(self, event):
        if (event.start_time >= self.start and event.start_time < self.end) or\
                (event.start_time <= self.start and
                event.end_time - 1 > self.start):
            self.events.append(event)

    def updated(self):
        self.last_update = datetime.now()

    def needs_update(self, timeout=timedelta(minutes=10)):
        return self.last_update + timeout < datetime.now()


class Event(object):
    def __init__(self, event_id, title, start_time, end_time, allday=False):
        self.event_id = event_id
        self.title = title
        self.start_time = start_time
        self.end_time = end_time
        self.allday = allday

    def __repr__(self):
        return '<Event: %r>' % (self.title)

class CalendarServer(dbus.service.Object):
    busname = 'org.gnome.Shell.CalendarServer'
    object_path = '/org/gnome/Shell/CalendarServer'

    def __init__(self, client):
        bus = dbus.service.BusName(self.busname,
                                        bus=dbus.SessionBus(),
                                        replace_existing=True)

        super(CalendarServer, self).__init__(bus, self.object_path)

        self.client = client
        self.calendars = self.get_calendars()

        # Events indexed by (since, until)
        self.months = {}

        self.updater = Thread()

        # Make threading work
        gtk.gdk.threads_init()

    def get_calendars(self):
        feed = self.client.GetAllCalendarsFeed()

        calendars = []
        urls = set()

        print feed.title.text + ':'

        for calendar in feed.entry:
            title = calendar.title.text
            url = calendar.content.src

            if not url in urls:
                print '  ', title
                print '    ', url
                urls.add(url)
                calendars.append((title, url))

        return calendars

    def parse_time(self, timestr):
        try:
            time = datetime.strptime(timestr, '%Y-%m-%d')
            time = time.timetuple()
            allday = True
        except ValueError:
            time = iso8601.parse_date(timestr)
            time = time.timetuple()[:-1] + (-1,) # Discard tm_isdst
            allday = False

        timestamp = int(mktime(time))

        return (timestamp, allday)

    def update_months_events(self, since_date, until_date, in_thread=False,
                            months_back=12, months_ahead=12):

        if in_thread:
            prefix = '      <<<<THREAD>>>>  '
        else:
            prefix = '    '

        print prefix, 'Update months events:', since_date, 'until',\
                until_date, '| months_back', months_back, '| months_ahead',\
                months_ahead

        months = {}

        since = int(mktime(since_date.timetuple()))
        until = int(mktime(until_date.timetuple()))

        min_date = since
        max_date = until

        key = (since, until)
        months[key] = MonthEvents(key, [])

        probe_date = since_date
        for i in range(0, months_back):
            probe_date -= timedelta(days=1)
            key = calendar_month_range(probe_date)
            months[key] = MonthEvents(key, [])
            probe_date = min_date = datetime.fromtimestamp(key[0])


        probe_date = until_date
        for i in range(0, months_ahead):
            probe_date += timedelta(days=1)
            key = calendar_month_range(probe_date)
            months[key] = MonthEvents(key, [])
            probe_date = max_date = datetime.fromtimestamp(key[1])

        # Get events from all calendars
        for calendar, feed_url in self.calendars:
            print prefix, 'Getting events from', calendar, '...'

            query = CalendarEventQuery()
            query.feed = feed_url
            query.start_min = min_date.strftime('%Y-%m-%d')
            query.start_max = max_date.strftime('%Y-%m-%d')
            query.max_results = 2**31-1
            feed = self.client.CalendarQuery(query)

            for event in feed.entry:
                event_id = event.id.text
                title = event.title.text

#                print prefix, '  ', title

                for when in event.when:
#                    print prefix, '    ', when.start_time, 'to', when.end_time

                    allday = False
                    start, allday = self.parse_time(when.start_time)
                    end, _ = self.parse_time(when.end_time)

                    e = Event(event_id, title, start, end, allday)
                    for _, month in months.items():
                        month.add_event(e)

        for key, month in months.items():
            month.updated()
            self.months[key] = month

        print prefix, '#Updated events since', min_date.strftime('%Y-%m-%d'), \
                'until', max_date.strftime('%Y-%m-%d')

    def need_update_near(self, key, months_back=4, months_ahead=4):
        """Checks if around month declared by `key` are old or not cahed
        months"""

        if self.months[key].needs_update():
            return True

        probe_date_back = datetime.fromtimestamp(key[0])
        probe_date_ahead = datetime.fromtimestamp(key[1])

        for i in range(0, months_back):
            probe_date_back -= timedelta(days=1)
            key = calendar_month_range(probe_date_back)
            month = self.months.get(key, None)
            if month:
                if self.months[key].needs_update():
                    return True
            else:
                return True
            probe_date_back = datetime.fromtimestamp(key[0])

        for i in range(0, months_ahead):
            probe_date_ahead += timedelta(days=1)
            key = calendar_month_range(probe_date_ahead)
            month = self.months.get(key, None)
            if month:
                if self.months[key].needs_update():
                    return True
            else:
                return True
            probe_date_ahead = datetime.fromtimestamp(key[1])
        return False

    @dbus.service.method('org.gnome.Shell.CalendarServer',
                         in_signature='xxb', out_signature='a(sssbxxa{sv})')
    def GetEvents(self, since, until, force_reload):
        since = int(since)
        until = int(until)
        force_reload = bool(force_reload)

        print "GetEvents(since=%s, until=%s, force_reload=%s)" % \
                (since, until, force_reload)

        probe_date = datetime.fromtimestamp(since) + timedelta(days=10)

        since, until = calendar_month_range(probe_date)

        since_date = datetime.fromtimestamp(since)
        until_date = datetime.fromtimestamp(until)
        print '  since', since_date, 'until', until_date

        key = (since, until)

        print '  key:', key, 'in months?', (key in self.months)

        if not key in self.months:
            print '  Month not yet downloaded'
            while self.updater.is_alive():
                print '  Waiting for updater thread to end...'
                sleep(1)
            if not key in self.months:
                print ' Month was\'nt downloaded by thread. Updating...'
                self.update_months_events(since_date, until_date)
            else:
                print '  Month was downloaded by thread'
        elif (not self.updater.is_alive()) and self.need_update_near(key):
            print '  Old cache. Starting updater thread...'
            self.updater = Thread(target=self.update_months_events,
                                    args=(since_date, until_date, True))
            self.updater.start()
        else:
            print '  Data loaded form cache'

        events = []

        for event in self.months[key].events:
            #print event.title

            events.append(('',               # uid
                           event.title,      # summary
                           '',               # description
                           event.allday,     # allDay
                           event.start_time, # date
                           event.end_time,   # end
                           {}))              # extras

        print ' Returning', len(events), 'events...'

        return events


def login(email, password):
    client = CalendarService()
    client.email = email
    client.password = password
    client.source = 'github-gnome_shell_google_calendar-0.1'
    client.ProgrammaticLogin()

    return client


def login_prompt():
    print 'Please enter your Google Calendar login information.'
    print 'The email and password will be stored securely in your keyring.'
    email = raw_input('E-mail: ')
    password = getpass('Password: ')

    return email, password


if __name__ == '__main__':
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    # Get credentials
    try:
        email, password = keyring.get_credentials()
    except keyring.KeyringError:
        email, password = login_prompt()
        keyring.set_credentials(email, password)

    # Login
    client = None
    while not client:
        try:
            print "Logging in as '%s'..." % email
            client = login(email, password)
        except Exception as e:
            print '%s.' % e
            email, password = login_prompt()
            keyring.set_credentials(email, password)

    myserver = CalendarServer(client)
    gtk.main()

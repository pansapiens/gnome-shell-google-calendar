#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from getpass import getpass
from threading import Thread
from time import mktime, sleep

from gdata.calendar.service import CalendarService, CalendarEventQuery
import dbus
import dbus.mainloop.glib
import dbus.service
import gtk
import iso8601
import keyring


def get_month_key(date, first_day_of_week=7):
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
    """
    Caches events of month
    """
    def __init__(self, key, events):
        self.start = key[0]
        self.end = key[1]
#        self.events = []
        self.gnome_events = []
        for event in events:
            self.add_event(event)
        self.last_update = datetime.now()

    def delete(self):
        print 'v'
        del self.start
        del self.end
#        del self.events[:]
        del self.gnome_events[:]
        del self.last_update
        print '^'

    def add_event(self, event):
        """Adds event to events and gnome_events if in month's range"""
        start = self.start
        end = self.end
        if (event.start_time >= start and event.start_time < end) or\
                (event.start_time <= start and event.end_time - 1 > start):
#            self.events.append(event)
            self.gnome_events.append(('',    # uid
                           event.title,      # summary
                           '',               # description
                           event.allday,     # allDay
                           event.start_time, # date
                           event.end_time,   # end
                           {}))              # extras

    def updated(self):
        self.last_update = datetime.now()

    def needs_update(self, timeout=timedelta(minutes=10)):
        return self.last_update + timeout < datetime.now()

    def get_key(self):
        return self.start, self.end

    def get_prev_month_key(self):
        probe_date = self.get_start_date() - timedelta(days=1)
        return get_month_key(probe_date)

    def get_next_month_key(self):
        probe_date = self.get_end_date() + timedelta(days=1)
        return get_month_key(probe_date)

    def get_start_date(self):
        return datetime.fromtimestamp(self.start)

    def get_end_date(self):
        return datetime.fromtimestamp(self.end)

    def __repr__(self):
        return u'<MonthEvents: %s, with %d events>' % (
                (self.get_start_date() + timedelta(days=10)).strftime('%B %Y'),
                len(self.gnome_events))


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

        # Make threading work
        gtk.gdk.threads_init()

        # Thread used to fetch events in background
        self.updater = Thread()

        # Thread keeping events updated
        self.scheduler = Thread(target=self.scheduler,
                                args=(timedelta(minutes=1),))
        self.scheduler.daemon = True
        self.scheduler.start()

    def scheduler(self, timeout):
        while 1:
            sleep(timeout.seconds)
            print 'Checking if actual month events need update...'
            if self.months[get_month_key(datetime.now())].\
                    needs_update(timedelta(minutes=2)):
                while self.updater.is_alive():
                    sleep(1)
                    print 'Scheduler waiting for updater thread to end...'
                if self.months[get_month_key(datetime.now())].\
                        needs_update(timedelta(minutes=2)):
                    print 'Scheduler starts updater thread...'
                    self.updater = Thread(target=self.update_months_events,
                                        args=(datetime.now(), True))
                    self.updater.start()
                else:
                    print 'Updater thread updated actual month'
            else:
                print 'No need for update'

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
#                print '    ', url
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

    def update_months_events(self, probe_date, in_thread=False,
                            months_back=12, months_ahead=12):
        if in_thread:
            prefix = '      <<<<THREAD>>>>  '
        else:
            prefix = '    '

        print prefix, 'Update months events around:',\
                probe_date.strftime('%B %Y'), '| months_back', months_back,\
                '| months_ahead', months_ahead

        months = {}

        # init asked month events
        key = initial_month_key = get_month_key(probe_date)
        months[key] = MonthEvents(key, [])

        # init previous months events
        for i in range(0, months_back):
            key = months[key].get_prev_month_key()
            months[key] = MonthEvents(key, [])
        # date for google query start limit
        min_date = months[key].get_start_date()

        # init next months events
        key = initial_month_key
        for i in range(0, months_ahead):
            key = months[key].get_next_month_key()
            months[key] = MonthEvents(key, [])
        # date for google query end limit
        max_date = months[key].get_end_date()

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

        # Replace old months events by new ones
        # TODO repair deletion if python doesn't do it
        for key, month in months.items():
            month.updated()
#            print '!'
#            self.months[key].delete()
#            print '!'
#            del self.months[key]
            self.months[key] = month

        print prefix, '#Updated events since', \
                (min_date + timedelta(days=10)).strftime('%B %Y'), \
                'until', (max_date - timedelta(days=10)).strftime('%B %Y')

    def need_update_near(self, _key, months_back=6, months_ahead=6):
        """Check if months around month declared by `key` need update or not
        yet fetched"""
        key = _key

        # Check if this month needs update
        if self.months[key].needs_update():
            return True

        # Check if previous months need update of not fetched
        for i in range(0, months_back):
            key = self.months[key].get_prev_month_key()
            month = self.months.get(key, None)
            if month:
                if self.months[key].needs_update():
                    return True
            else:
                return True

        # Check if next months need update of not fetched
        key = _key
        for i in range(0, months_ahead):
            key = self.months[key].get_next_month_key()
            month = self.months.get(key, None)
            if month:
                if self.months[key].needs_update():
                    return True
            else:
                return True

        # All up to date
        return False

    @dbus.service.method('org.gnome.Shell.CalendarServer',
                         in_signature='xxb', out_signature='a(sssbxxa{sv})')
    def GetEvents(self, since, until, force_reload):
        since = int(since)
        until = int(until)
        force_reload = bool(force_reload)

        print "\nGetEvents(since=%s, until=%s, force_reload=%s)" % \
                (since, until, force_reload)

        probe_date = datetime.fromtimestamp(since) + timedelta(days=10)

        print '  Getting events for:', probe_date.strftime('%B %Y')

        key = get_month_key(probe_date)

        if not key in self.months:
            print '  Month not yet downloaded'
            while self.updater.is_alive():
                print '  Waiting for updater thread to end...'
                sleep(1)
            if not key in self.months:
                print '  Updating...'
                self.update_months_events(probe_date)
            else:
                print '  Month was downloaded by thread'
        elif (not self.updater.is_alive()) and self.need_update_near(key):
            print '  Old cache. Starting updater thread...'
            self.updater = Thread(target=self.update_months_events,
                                    args=(probe_date, True))
            self.updater.start()
        else:
            print '  Data loaded form cache'

        print ' #Returning', len(self.months[key].gnome_events), 'events...'

        return self.months[key].gnome_events


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

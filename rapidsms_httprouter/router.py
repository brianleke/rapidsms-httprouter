from django.conf import settings
from .models import Message
from rapidsms.models import Backend, Connection
from rapidsms.apps.base import AppBase
from rapidsms.messages.incoming import IncomingMessage
from rapidsms.log.mixin import LoggerMixin
from threading import Lock

from urllib import quote_plus
from urllib2 import urlopen

class HttpRouter(object, LoggerMixin):
    """
    This is a simplified version of the normal SMS router in that it has no threading.  Instead
    it is expected that the handle_incoming and handle_outcoming calls are made in the HTTP
    thread.
    """

    incoming_phases = ("filter", "parse", "handle", "default", "cleanup")
    outgoing_phases = ("outgoing",)

    def __init__(self):
        # the apps we'll run through
        self.apps = []

        # we need to be started
        self.started = False

    def add_message(self, backend, contact, text, direction, status):
        """
        Adds this message to the db.  This is both for logging, and we also keep state
        tied to it.
        """
        # lookup / create this backend
        # TODO: is this too flexible?  Perhaps we should do this upon initialization and refuse 
        # any backends not found in our settings.  But I hate dropping messages on the floor.
        backend, created = Backend.objects.get_or_create(name=backend)
        
        # create our connection
        connection, created = Connection.objects.get_or_create(backend=backend, identity=contact)
        print "CREATING CONNECTION %s %s" % (contact, created)
        # finally, create our db message
        message = Message.objects.create(connection=connection,
                                         text=text,
                                         direction='I',
                                         status=status)
        return message


    def mark_sent(self, message_id):
        """
        Marks a message as sent by the backend.
        """
        message = Message.objects.get(pk=message_id)
        message.status = 'S'
        message.save()


    def handle_incoming(self, backend, sender, text):
        """
        Handles an incoming message.
        """
        # create our db message for logging
        db_message = self.add_message(backend, sender, text, 'I', 'R')

        # and our rapidsms transient message for processing
        msg = IncomingMessage(db_message.connection, text, db_message.date)
        
        # add an extra property to IncomingMessage, so httprouter-aware
        # apps can make use of it during the handling phase
        msg.db_message = db_message
        
        self.info("Incoming (%s): %s" % (msg.connection, msg.text))
        print ("Incoming (%s): %s" % (msg.connection, msg.text))
        try:
            for phase in self.incoming_phases:
                self.debug("In %s phase" % phase)
                if phase == "default":
                    if msg.handled:
                        self.debug("Skipping phase")
                        break

                for app in self.apps:
                    self.debug("In %s app" % app)
                    handled = False

                    try:
                        func = getattr(app, phase)
                        handled = func(msg)

                    except Exception, err:
                        import traceback
                        traceback.print_exc(err)
                        app.exception()

                    # during the _filter_ phase, an app can return True
                    # to abort ALL further processing of this message
                    if phase == "filter":
                        if handled is True:
                            self.warning("Message filtered")
                            raise(StopIteration)

                    # during the _handle_ phase, apps can return True
                    # to "short-circuit" this phase, preventing any
                    # further apps from receiving the message
                    elif phase == "handle":
                        if handled is True:
                            self.debug("Short-circuited")
                            # mark the message handled to avoid the 
                            # default phase firing unnecessarily
                            msg.handled = True
                            break
                    
                    elif phase == "default":
                        # allow default phase of apps to short circuit
                        # for prioritized contextual responses.   
                        if handled is True:
                            self.debug("Short-circuited default")
                            break
                        
        except StopIteration:
            pass

        db_message.status = 'H'
        db_message.save()

        db_responses = []

        # now send the message responses
        while msg.responses:
            response = msg.responses.pop(0)
            self.handle_outgoing(response, db_message)

        # we are no longer interested in this message... but some crazy
        # synchronous backends might be, so mark it as processed.
        msg.processed = True

        return db_message


    def add_outgoing(self, connection, text, source=None, status='Q'):
        """
        Adds a message to our outgoing queue, this is a non-blocking action
        """
        db_message = Message.objects.create(connection=connection,
                                            text=text,
                                            direction='O',
                                            status=status,
                                            in_response_to=source)
        return db_message
                
    def handle_outgoing(self, msg, source=None):
        """
        Passes the message through the appropriate outgoing steps for all our apps,
        then sends it off if it wasn't cancelled.
        """
        
        # first things first, add it to our db/queue
        db_message = self.add_outgoing(msg.connection, msg.text, source, status='P')

        #FIXME: check for available worker threads in the pool, add one if necessary
        #FIXME: move below code to worker thread run method
        self.info("Outgoing (%s): %s" % (msg.connection, msg.text))

        for phase in self.outgoing_phases:
            self.debug("Out %s phase" % phase)
            continue_sending = True

            # call outgoing phases in the opposite order of the incoming
            # phases, so the first app called with an  incoming message
            # is the last app called with an outgoing message
            for app in reversed(self.apps):
                self.debug("Out %s app" % app)

                try:
                    func = getattr(app, phase)
                    continue_sending = func(msg)

                except Exception, err:
                    app.exception()

                # during any outgoing phase, an app can return True to
                # abort ALL further processing of this message
                if continue_sending is False:
                    db_message.status = 'C'
                    db_message.save()
                    self.warning("Message cancelled")
                    return False

        # add the message to our outgoing queue
        self.send_message(db_message)
        
        #FIXME: add above code to worker thread
        return db_message

    def send_message(self, msg, **kwargs):
        """
        Sends the message off.  We first try to directly contact our sms router to deliver it, 
        if we fail, then we just add it to our outgoing queue.
        """

        if not getattr(settings, 'ROUTER_URL', None):
            print "No ROUTER_URL set in settings.py, queuing message for later delivery."
            msg.status = 'Q'
            msg.save()
            return

        params = {
            'backend': msg.connection.backend,
            'recipient': msg.connection.identity,
            'text': msg.text,
            'id': msg.pk
        }

        # add any other backend-specific parameters from kwargs
        params.update(kwargs)
        
        for k, v in params.items():
            params[k] = quote_plus(str(v))
        try:
            #FIXME: clean this up
            response = urlopen(settings.ROUTER_URL % params)

            # kannel likes to send 202 responses, really any
            # 2xx value means things went okay
            if int(response.getcode()/100) == 2:
                self.info("Message: %s sent: " % msg.id)
                msg.status = 'S'
                msg.save()
            else:
                self.error("Message not sent, got status: %s .. queued for later delivery." % response.getcode())
                msg.status = 'Q'
                msg.save()

        except Exception as e:
            self.error("Message not sent: %s .. queued for later delivery." % str(e))
            msg.status = 'Q'
            msg.save()

    def add_app(self, module_name):
        """
        Find the app named *module_name*, instantiate it, and add it to
        the list of apps to be notified of incoming messages. Return the
        app instance.
        """
        try:
            cls = AppBase.find(module_name)
        except:
            cls = None

        if cls is None: return None

        app = cls(self)
        self.apps.append(app)
        return app


    def start(self):
        """
        Initializes our router.
        TODO: this happens in the HTTP thread on the first call, that could be bad.
        """

        # add all our apps
        for app_name in settings.SMS_APPS:
            self.add_app(app_name)

        # start all our apps
        for app in self.apps:
            app.start()

        # the list of messages which need to be sent, we load this from the DB
        # upon first starting up
        self.outgoing = [message for message in Message.objects.filter(status='Q')]

        # mark ourselves as started
        self.started = True
        
# we'll get started when we first get used
http_router = HttpRouter()
http_router_lock = Lock()

def get_router():
    """
    Takes care of performing lazy initialization of the www router.
    """
    global http_router
    global http_router_lock

    if not http_router.started:
        http_router_lock.acquire()
        try:
            if not http_router.started:
                http_router.start()
        finally:
            http_router_lock.release()

    return http_router

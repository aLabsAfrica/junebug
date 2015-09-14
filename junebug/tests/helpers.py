from copy import deepcopy
import logging
import logging.handlers
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.trial.unittest import TestCase
from twisted.web.server import Site
from txamqp.client import TwistedDelegate
from vumi.utils import vumi_resource_path
from vumi.service import get_spec
from vumi.tests.fake_amqp import FakeAMQPBroker, FakeAMQPChannel
from vumi.tests.helpers import PersistenceHelper

from junebug import JunebugApi
from junebug.amqp import JunebugAMQClient, MessageSender
from junebug.channel import Channel
from junebug.service import JunebugService
from junebug.config import JunebugConfig


class FakeAmqpClient(JunebugAMQClient):
    '''Amqp client, base upon the real JunebugAMQClient, that uses a
    FakeAMQPBroker instead of a real broker'''
    def __init__(self, spec):
        super(FakeAmqpClient, self).__init__(TwistedDelegate(), '', spec)
        self.broker = FakeAMQPBroker()

    @inlineCallbacks
    def channel(self, id):
        yield self.channelLock.acquire()
        try:
            try:
                ch = self.channels[id]
            except KeyError:
                ch = FakeAMQPChannel(id, self)
                self.channels[id] = ch
        finally:
            self.channelLock.release()
        returnValue(ch)


class JunebugTestBase(TestCase):
    '''Base test case that all junebug tests inherit from. Contains useful
    helper functions'''

    default_channel_config = {
        'type': 'telnet',
        'config': {
            'twisted_endpoint': 'tcp:0',
            'worker_name': 'unnamed',
        },
        'mo_url': 'http://foo.bar',
    }

    def patch_logger(self):
        ''' Patches the logger with an in-memory logger, which is acccessable
        at "self.logging_handler".'''
        self.logging_handler = logging.handlers.MemoryHandler(100)
        logging.getLogger().addHandler(self.logging_handler)
        self.addCleanup(self._cleanup_logging_patch)

    def _cleanup_logging_patch(self):
        self.logging_handler.close()
        logging.getLogger().removeHandler(self.logging_handler)

    def create_channel_config(self, **kw):
        config = deepcopy(self.default_channel_config)
        config.update(kw)
        return config

    @inlineCallbacks
    def create_channel(
            self, service, redis, transport_class,
            config=default_channel_config, id=None):
        '''Creates and starts, and saves a channel, with a
        TelnetServerTransport transport'''
        config = deepcopy(config)
        channel = Channel(redis, {}, config, id=id)
        config['config']['transport_name'] = channel.id
        yield channel.start(self.service)
        yield channel.save()
        self.addCleanup(channel.stop)
        returnValue(channel)

    def create_channel_from_id(self, service, redis, id):
        '''Creates an existing channel given the channel id'''
        return Channel.from_id(redis, {}, id, service)

    @inlineCallbacks
    def get_redis(self):
        '''Creates and returns a redis manager'''
        if hasattr(self, 'redis'):
            returnValue(self.redis)
        persistencehelper = PersistenceHelper()
        yield persistencehelper.setup()
        self.redis = yield persistencehelper.get_redis_manager()
        self.addCleanup(persistencehelper.cleanup)
        returnValue(self.redis)

    @inlineCallbacks
    def start_server(self):
        '''Starts a junebug server. Stores the service to "self.service", and
        the url at "self.url"'''
        redis = yield self.get_redis()
        self.service = JunebugService(JunebugConfig({
            'host': '127.0.0.1',
            'port': 0,
            'redis': redis._config,
            'amqp': {
                'hostname': '',
                'port': ''
            }
        }))
        self.api = JunebugApi(
            self.service, redis._config, {'hostname': '', 'port': ''})
        self.api.redis = redis

        self.api.message_sender = self.get_message_sender()

        port = reactor.listenTCP(
            0, Site(self.api.app.resource()),
            interface='127.0.0.1')
        self.addCleanup(port.stopListening)
        addr = port.getHost()
        self.url = "http://%s:%s" % (addr.host, addr.port)

    def get_message_sender(self):
        '''Creates a new MessageSender object, with a fake amqp client'''
        message_sender = MessageSender('amqp-spec-0-8.xml', None)
        spec = get_spec(vumi_resource_path('amqp-spec-0-8.xml'))
        client = FakeAmqpClient(spec)
        message_sender.client = client
        return message_sender

    def get_dispatched_messages(self, queue):
        '''Gets all messages that have been dispatched to the amqp broker.
        Should only be called after start_server, as it looks in the api for
        the amqp client'''
        amqp_client = self.api.message_sender.client
        return amqp_client.broker.get_messages(
            'vumi', queue)

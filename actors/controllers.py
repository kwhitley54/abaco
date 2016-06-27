import json

from flask import g, request
from flask_restful import Resource, Api, inputs

from channels import ActorMsgChannel, CommandChannel
from codes import SUBMITTED, PERMISSION_LEVELS
from errors import PermissionsException, WorkerException
from models import Actor, Execution, Worker, get_permissions, add_permission
from request_utils import RequestParser, APIException, ok
from stores import actors_store, executions_store, logs_store, permissions_store
from worker import shutdown_workers, shutdown_worker


class ActorsResource(Resource):

    def get(self):
        actors = []
        for k, v in actors_store.items():
            if v['tenant'] == g.tenant:
                actors.append(Actor.from_db(v).display())
        return ok(result=actors, msg="Actors retrieved successfully.")

    def validate_post(self):
        parser = Actor.request_parser()
        return parser.parse_args()

    def post(self):
        args = self.validate_post()
        args['tenant'] = g.tenant
        actor = Actor(**args)
        actors_store[actor.db_id] = actor.to_db()
        ch = CommandChannel()
        ch.put_cmd(actor_id=actor.db_id, image=actor.image, tenant=args['tenant'])
        add_permission(g.user, actor.db_id, 'UPDATE')
        return ok(result=actor.display(), msg="Actor created successfully.")


class ActorResource(Resource):
    def get(self, actor_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        try:
            actor = Actor.from_db(actors_store[dbid])
        except KeyError:
            raise APIException(
                "actor not found: {}. db_id:{}'".format(actor_id, dbid), 404)
        return ok(result=actor.display(), msg="Actor retrieved successfully.")

    def delete(self, actor_id):
        id = Actor.get_dbid(g.tenant, actor_id)
        shutdown_workers(id)
        try:
            actor = Actor.from_db(actors_store[id])
            executions = actor.get('executions') or {}
            for ex_id, val in executions.items():
                del logs_store[ex_id]
        except KeyError:
            print("Did not find actor with id: {}".format(id))
        del actors_store[id]
        del permissions_store[id]
        return ok(result=None, msg='Actor deleted successfully.')

    def put(self, actor_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        try:
            actor = Actor.from_db(actors_store[dbid])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        previous_image = actor.image
        args = self.validate_put(actor)
        args['tenant'] = g.tenant
        update_image = False
        if args['image'] == previous_image:
            args['status'] = actor.status
        else:
            update_image = True
            args['status'] = SUBMITTED
        actor = Actor(**args)
        actors_store[actor.db_id] = actor.to_db()
        if update_image:
            ch = CommandChannel()
            ch.put_cmd(actor_id=actor.db_id, image=actor.image, tenant=args['tenant'])
        # return ok(result={'update_image': str(update_image)},
        #           msg="Actor updated successfully.")
        return ok(result=actor.display(),
                  msg="Actor updated successfully.")

    def validate_put(self, actor):
        # inherit derived attributes from the original actor, including id and db_id:
        parser = Actor.request_parser()
        # remove since name is only required for POST, not PUT
        parser.remove_argument('name')
        # this update overrides all required and optional attributes
        actor.update(parser.parse_args())
        return actor

    def delete_actor_message(self, actor_id):
        """Put a command message on the actor_messages queue that actor was deleted."""
        # TODO
        pass


class ActorStateResource(Resource):
    def get(self, actor_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        try:
            actor = Actor.from_db(actors_store[dbid])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        return ok(result={'state': actor.get('state') }, msg="Actor state retrieved successfully.")

    def post(self, actor_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        args = self.validate_post()
        state = args['state']
        try:
            actor = Actor.from_db(actors_store[dbid])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        actors_store.update(dbid, 'state', state)
        return ok(result=actor.display(), msg="State updated successfully.")

    def validate_post(self):
        parser = RequestParser()
        parser.add_argument('state', type=str, required=True, help="Set the state for this actor.")
        args = parser.parse_args()
        return args


class ActorExecutionsResource(Resource):
    def get(self, actor_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        try:
            actors_store[dbid]
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        tot = {'total_executions': 0, 'total_cpu': 0, 'total_io':0, 'total_runtime': 0, 'ids':[]}
        try:
            executions = executions_store[dbid]
        except KeyError:
            executions = {}
        for id, val in executions.items():
            tot['ids'].append(id)
            tot['total_executions'] += 1
            tot['total_cpu'] += int(val['cpu'])
            tot['total_io'] += int(val['io'])
            tot['total_runtime'] += int(val['runtime'])
        return ok(result=tot, msg="Actor executions retrieved successfully.")

    def post(self, actor_id):
        id = Actor.get_dbid(g.tenant, actor_id)
        try:
            actor = Actor.from_db(actors_store[id])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        args = self.validate_post()
        Execution.add_execution(id, args)
        return ok(result=actor.display(), msg="Actor execution added successfully.")

    def validate_post(self):
        parser = RequestParser()
        parser.add_argument('runtime', type=str, required=True, help="Runtime, in milliseconds, of the execution.")
        parser.add_argument('cpu', type=str, required=True, help="CPU usage, in user jiffies, of the execution.")
        parser.add_argument('io', type=str, required=True, help="Block I/O usage, in number of 512-byte sectors read from and written to, by the execution.")
        # Accounting for memory is quite hard -- probably easier to cap all containers at a fixed amount or perhaps have
        # a graduated list of cap sized (e.g. small, medium and large).
        # parser.add_argument('mem', type=str, required=True, help="Memory usage, , of the execution.")
        args = parser.parse_args()
        for k,v in args.items():
            try:
                int(v)
            except ValueError:
                raise APIException(message="Argument " + k + " must be an integer.")
        return args


class ActorExecutionResource(Resource):
    def get(self, actor_id, execution_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        try:
            actors_store[dbid]
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        try:
            excs = executions_store[dbid]
        except KeyError:
            raise APIException("No executions found for actor {}.".format(actor_id))
        try:
            exc = Execution.from_db(excs[execution_id])
        except KeyError:
            raise APIException("Execution not found {}.".format(execution_id))
        return ok(result=exc.display(), msg="Actor execution retrieved successfully.")


class ActorExecutionLogsResource(Resource):
    def get(self, actor_id, execution_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        try:
            actors_store[dbid]
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        try:
            executions_store[dbid]
        except KeyError:
            raise APIException("No executions found for actor {}.".format(actor_id))
        try:
            logs = logs_store[execution_id]
        except KeyError:
            logs = ""
        return ok(result=logs, msg="Logs retrieved successfully.")


class MessagesResource(Resource):

    def get(self, actor_id):
        # check that actor exists
        id = Actor.get_dbid(g.tenant, actor_id)
        try:
            actor = Actor.from_db(actors_store[id])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        # TODO
        # retrieve pending messages from the queue
        return ok(result={'messages': len(ActorMsgChannel(actor_id=id)._queue._queue)})

    def validate_post(self):
        parser = RequestParser()
        parser.add_argument('message', type=str, required=True, help="The message to send to the actor.")
        args = parser.parse_args()
        return args

    def post(self, actor_id):
        args = self.validate_post()
        d = {}
        # build a dictionary of k:v pairs from the query parameters, and pass a single
        # additional object 'message' from within the post payload. Note that 'message'
        # need not be JSON data.
        for k, v in request.args.items():
            if k == 'message':
                continue
            d[k] = v
        if hasattr(g, 'user'):
            d['_abaco_username'] = g.user
        if hasattr(g, 'jwt'):
            d['_abaco_jwt'] = g.jwt
        if hasattr(g, 'api_server'):
            d['_abaco_api_server'] = g.api_server
        dbid = Actor.get_dbid(g.tenant, actor_id)
        # create an execution
        exc = Execution.add_execution(dbid, {'cpu': 0, 'io': 0, 'runtime': 0, 'status': SUBMITTED})
        d['_abaco_execution_id'] = exc
        ch = ActorMsgChannel(actor_id=dbid)
        ch.put_msg(message=args['message'], d=d)
        # make sure at least one worker is available
        workers = Worker.get_workers(dbid)
        if len(workers.items()) < 1:
            ch = CommandChannel()
            actor = Actor.from_db(actors_store[dbid])
            ch.put_cmd(actor_id=dbid, image=actor.image, tenant=g.tenant, num=1, stop_existing=False)
        return ok(result={'execution_id': exc,
                          'msg': args['message']})


class WorkersResource(Resource):
    def get(self, actor_id):
        dbid = Actor.get_dbid(g.tenant, actor_id)
        try:
            Actor.from_db(actors_store[dbid])
        except KeyError:
            raise APIException("actor not found: {}'".format(actor_id), 400)
        try:
            workers = Worker.get_workers(dbid)
        except WorkerException as e:
            raise APIException(e.msg, 404)
        return ok(result=workers, msg="Workers retrieved successfully.")

    def validate_post(self):
        parser = RequestParser()
        parser.add_argument('num', type=int, help="Number of workers to start (default is 1).")
        args = parser.parse_args()
        return args

    def post(self, actor_id):
        """Start new workers for an actor"""
        id = Actor.get_dbid(g.tenant, actor_id)
        try:
            actor = Actor.from_db(actors_store[id])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        args = self.validate_post()
        num = args.get('num')
        if not num or num == 0:
            num = 1
        ch = CommandChannel()
        ch.put_cmd(actor_id=actor.db_id, image=actor.image, tenant=g.tenant, num=num, stop_existing=False)
        return ok(result=None, msg="Scheduled {} new worker(s) to start.".format(str(num)))


class WorkerResource(Resource):
    def get(self, actor_id, ch_name):
        id = Actor.get_dbid(g.tenant, actor_id)
        try:
            Actor.from_db(actors_store[id])
        except KeyError:
            raise WorkerException("actor not found: {}'".format(actor_id))
        try:
            worker = Worker.get_worker(id, ch_name)
        except WorkerException as e:
            raise APIException(e.msg, 404)
        return ok(result=worker, msg="Worker retrieved successfully.")

    def delete(self, actor_id, ch_name):
        id = Actor.get_dbid(g.tenant, actor_id)
        try:
            worker = Worker.get_worker(id, ch_name)
        except WorkerException as e:
            raise APIException(e.msg, 404)
        shutdown_worker(ch_name)
        return ok(result=worker, msg="Worker scheduled to be stopped.")


class PermissionsResource(Resource):
    def get(self, actor_id):
        id = Actor.get_dbid(g.tenant, actor_id)
        try:
            Actor.from_db(actors_store[id])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        try:
            permissions = get_permissions(id)
        except PermissionsException as e:
            raise APIException(e.msg, 404)
        return ok(result=permissions, msg="Permissions retrieved successfully.")

    def validate_post(self):
        parser = RequestParser()
        parser.add_argument('user', type=str, required=True, help="User owning the permission.")
        parser.add_argument('level', type=str, required=True,
                            help="Level of the permission: {}".format(PERMISSION_LEVELS))
        args = parser.parse_args()
        if not args['level'] in PERMISSION_LEVELS:
            raise APIException("Invalid permission level: {}. \
            The valid values are {}".format(args['level'], PERMISSION_LEVELS))
        return args

    def post(self, actor_id):
        """Add new permissions for an actor"""
        id = Actor.get_dbid(g.tenant, actor_id)
        try:
            Actor.from_db(actors_store[id])
        except KeyError:
            raise APIException(
                "actor not found: {}'".format(actor_id), 404)
        args = self.validate_post()
        add_permission(args['user'], id, args['level'])
        permissions = get_permissions(id)
        return ok(result=permissions, msg="Permission added successfully.")

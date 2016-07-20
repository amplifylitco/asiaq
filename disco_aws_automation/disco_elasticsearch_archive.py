from ConfigParser import NoOptionError
from collections import defaultdict
from time import time, sleep
import json
import logging
import re
import os

import boto3
from boto3.session import Session
from elasticsearch import (
    Elasticsearch,
    NotFoundError,
    RequestsHttpConnection,
    TransportError,
)
from requests_aws4auth import AWS4Auth

from . import read_config, DiscoElasticsearch, DiscoIAM
from .exceptions import TimeoutError
from .disco_constants import (
    ES_ARCHIVE_LIFECYCLE_DIR,
    ES_CONFIG_FILE
)

ES_SNAPSHOT_FINAL_STATES = ['SUCCESS', 'FAILED']
SNAPSHOT_WAIT_TIMEOUT = 60*60
SNAPSHOT_POLL_INTERVAL = 60
ES_ARCHIVE_ROLE = "es_archive"


class DiscoESArchive(object):
    def __init__(self, environment_name, cluster_name, config_aws=None,
                 config_es=None,
                 index_prefix_pattern=None, repository_name=None, disco_es=None,
                 disco_iam=None):
        self.config_aws = config_aws or read_config()
        self.config_es = config_es or read_config(ES_CONFIG_FILE)

        if environment_name:
            self.environment_name = environment_name.lower()
        else:
            self.environment_name = self.config_aws.get("disco_aws", "default_environment")

        self.cluster_name = cluster_name
        self._host = None
        self._es_client = None
        self._region = None
        self._s3_client = None
        self._s3_bucket_name = None
        self._disco_es = disco_es
        self._disco_iam = disco_iam
        self.index_prefix_pattern = index_prefix_pattern or r'.*'
        self.repository_name = repository_name or 's3'

    @property
    def host(self):
        """
        Return ES cluster hostname
        """
        if not self._host:
            es_domains = self.disco_es.list()
            try:
                host = [es_domain["route_53_endpoint"]
                        for es_domain in es_domains
                        if es_domain.get("internal_name") == self.cluster_name][0]
            except IndexError:
                logging.info("Unable to find")
                raise RuntimeError("Unable to find ES cluster: {}".format(self.cluster_name))
            self._host = {'host': host, 'port': 80}
        return self._host

    @property
    def region(self):
        """
        Current region, based of boto connection.
        """
        if not self._region:
            self._region = Session().region_name
        return self._region

    @property
    def role_arn(self):
        """
        Return aws role_arn
        """
        arn = self.disco_iam.get_role_arn(ES_ARCHIVE_ROLE)
        if not arn:
            raise RuntimeError("Unable to find ARN for role: {}".format(ES_ARCHIVE_ROLE))

        return arn

    @property
    def es_client(self):
        """
        Return authenticated ElasticSeach connection object
        """
        if not self._es_client:
            session = Session()
            credentials = session.get_credentials()
            aws_auth = AWS4Auth(
                credentials.access_key,
                credentials.secret_key,
                self.region,
                'es'
            )
            self._es_client = Elasticsearch(
                [self.host],
                http_auth=aws_auth,
                use_ssl=False,
                verify_certs=False,
                connection_class=RequestsHttpConnection,
            )
        return self._es_client

    @property
    def disco_es(self):
        if not self._disco_es:
            self._disco_es = DiscoElasticsearch(self.environment_name)

        return self._disco_es

    @property
    def disco_iam(self):
        if not self._disco_iam:
            # TODO: the environment argument for DiscoIAM is actually the account
            # name, e.g. "dev" or "prod"
            self._disco_iam = DiscoIAM(environment=self.environment_name)

        return self._disco_iam

    @property
    def s3_client(self):
        if not self._s3_client:
            self._s3_client = boto3.client("s3")

        return self._s3_client

    @property
    def s3_bucket_name(self):
        if not self._s3_bucket_name:
            self._s3_bucket_name = "{0}.es-{1}-archive.{2}".format(
                self.region, self.cluster_name,
                self.environment_name)

        return self._s3_bucket_name

    def _bytes_to_free(self, threshold):
        """
        Number of bytes disk space threshold is exceeded by. In other
        words, number of bytes of data that needs to be delted to drop
        below threshold.
        threshold range 0-1
        """
        cluster_stats = self.es_client.cluster.stats()
        total_bytes = cluster_stats["nodes"]["fs"]["total_in_bytes"]
        free_bytes = cluster_stats["nodes"]["fs"]['free_in_bytes']
        need_bytes = total_bytes * (1 - threshold)
        logging.debug("disk utilization : %i%%", (total_bytes-free_bytes)*101/total_bytes)
        return need_bytes - free_bytes

    def _archivable_indices(self, threshold):
        """
        Return list of indices that ough to be archived to get below
        max disk space threshold. Oldest first.
        """
        bytes_to_free = self._bytes_to_free(threshold)
        logging.debug("Need to free %i bytes.", bytes_to_free)
        if bytes_to_free <= 0:
            return []

        stats = self.es_client.indices.stats()
        dated_index_pattern = re.compile(
            self.index_prefix_pattern + r'-[\d]{4}(\.[\d]{2}){2}$'
        )
        index_sizes = [
            (index, stats['indices'][index]['total']['store']['size_in_bytes'])
            for index in sorted(stats['indices'], key=lambda k: k[-10:])
            if re.match(dated_index_pattern, index)
        ]

        archivable_indices = []
        freed_size = 0
        for index, size in index_sizes:
            if freed_size > bytes_to_free:
                break
            archivable_indices.append(index)
            freed_size += size
        return archivable_indices

    def _green_indices(self):
        """
        Return list of indexes that are in green state (healthy)
        """
        index_health = self.es_client.cluster.health(level='indices')
        return [
            index
            for index in index_health['indices'].keys()
            if index_health['indices'][index]['status'] == 'green'
        ]

    def _create_repository(self):
        """
        Creates a s3 type repository where archives can be stored
        and retrieved.
        """
        # Update S3 bucket based on config.
        self._update_s3_bucket(self.s3_bucket_name)

        repository_config = {
            "type": "s3",
            "settings": {
                "bucket": self.s3_bucket_name,
                "region": self.region,
                "role_arn": self.role_arn,
            }
        }

        logging.info("Creating ES snapshot repository: %s", self.repository_name)
        try:
            self.es_client.snapshot.create_repository(self.repository_name, repository_config)
        except TransportError:
            try:
                self.es_client.snapshot.get_repository('s3')
                # This can happen when repo is used to make snapshot but we
                # attempt to re-create it.
                logging.warning(
                    "Error on creating ES snapshot respository %s, "
                    "but one already exists, ingnoring",
                    self.repository_name
                )
            except:
                logging.error(
                    "Could not create archive repository. "
                    "Make sure bucket %s exists and IAM policy allows es to access bucket. "
                    "repository config: %s",
                    bucket_name,
                    repository_config,
                )
                raise

    def _update_s3_bucket(self, bucket_name):
        # Auto create bucket if necessary
        buckets = self.s3_client.list_buckets()['Buckets']
        if bucket_name not in [bucket['Name'] for bucket in buckets]:
            logging.info("Creating bucket %s", bucket_name)

            self.s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={
                    'LocationConstraint': self.region})

        s3_archive_policy_name = self.get_es_option_default(
            's3_archive_policy', 'default')

        # Apply the lifecycle policy, if one exists
        lifecycle_policy = self._load_policy_json(s3_archive_policy_name, 'lifecycle')
        if lifecycle_policy:
            logging.info("Applying S3 lifecycle policy ({0}) to bucket ({1}).".format(
                s3_archive_policy_name, bucket_name))
            self.s3_client.put_bucket_lifecycle_configuration(
                Bucket=bucket_name, LifecycleConfiguration=lifecycle_policy)

        # Apply the bucket policy, if one exists
        bucket_policy = self._load_policy_json(s3_archive_policy_name, 'iam')
        if bucket_policy:
            logging.info("Applying S3 bucket policy ({0}) to bucket ({1}).".format(
                s3_archive_policy_name, bucket_name))
            self.s3_client.put_bucket_policy(
                Bucket=bucket_name, Policy=bucket_policy)

        # Apply the bucket access policy, if one exists
        bucket_access_policy = self._load_policy_json(s3_archive_policy_name, 'acp')
        if bucket_access_policy:
            logging.info("Applying S3 bucket access policy ({0}) to bucket ({1}).".format(
                s3_archive_policy_name, bucket_name))
            self.s3_client.put_bucket_acl(
                Bucket=bucket_name, AccessControlPolicy=bucket_access_policy)

        # Apply versioning configuration, if one exists
        version_config = self._load_policy_json(s3_archive_policy_name, 'versioning')
        if version_config:
            logging.info("Applying S3 versioning config ({0}) to bucket ({1}).".format(
                s3_archive_policy_name, bucket_name))
            self.s3_client.put_bucket_versioning(
                Bucket=bucket_name, VersioningConfiguration=version_config)

        # Set the logging policy, if one exists
        logging_policy = self._load_policy_json(s3_archive_policy_name, 'logging')
        if logging_policy:
            logging.info("Applying S3 logging policy ({0}) to bucket ({1}).".format(
                s3_archive_policy_name, bucket_name))
            logging.info("policy: {0}".format(logging_policy))
            self.s3_client.put_bucket_logging(
                Bucket=bucket_name, BucketLoggingStatus=logging_policy)


    def _load_policy_json(self, policy_name, policy_type):
        policy_file = "{0}/{1}.{2}".format(
            ES_ARCHIVE_LIFECYCLE_DIR, policy_name, policy_type)

        if os.path.isfile(policy_file):
            with open(policy_file, 'r') as opened_file:
                text = opened_file.read().replace('\n', '')
                return json.loads(self._interpret_file(text))

        return None

    def _interpret_file(self, file_txt):
        return file_txt.replace('$REGION', self.region) \
                       .replace('$CLUSTER', self.cluster_name) \
                       .replace('$BUCKET_NAME', self.s3_bucket_name)

    def get_es_option(self, option):
        """Returns appropriate configuration for the current environment"""
        section = "{}:{}".format(self.environment_name, self.cluster_name)

        if self.config_es.has_option(section, option):
            return self.config_es.get(section, option)

        raise NoOptionError(option, section)

    def get_es_option_default(self, option, default=None):
        """Returns appropriate configuration for the current environment"""
        try:
            return self.get_es_option(option)
        except NoOptionError:
            return default

    def archive(self):
        """
        Archive enough indexes to bring down disk usage to specified threshold.
        Archive means move to s3 and delete from es cluster.
        """
        threshold = float(self.get_es_option("archive_threshold"))
        if threshold > 1.0:
            raise RuntimeError("ElasticSearch archive threshold cannot exceed 1")

        indices = self._archivable_indices(threshold)
        green_indices = self._green_indices()
        green_archivable = set(indices) & set(green_indices)
        ungreen_archivable = set(indices) - set(green_indices)
        if ungreen_archivable:
            logging.error(
                "Skipping archiving of following unhealthy indexes: %s",
                ", ".join(ungreen_archivable)
            )
        if not green_archivable:
            logging.warning("No indexes to archive.")
            return

        self._create_repository()

        green_archivable = set([list(green_archivable)[11]])
        snap_states = defaultdict(list)
        snap_states['skipped'] = list(ungreen_archivable)

        for index in green_archivable:
            if self.snapshot_state(index) != 'unknown':
                logging.error(
                    'Index archive with conflicting name %s exists, skipping archiving',
                    index
                )
                # TODO optionally delete snapshot for overwriting? 
                snap_states['skipped'].append(index)
                continue

            logging.info("Archiving index: %s", index)
            self.es_client.snapshot.create(
                self.repository_name,
                index,
                {
                    "indices": index,
                    "settings": {
                        "role_arn": self.role_arn
                    }
                }
            )
            snap_state = self._wait_for_snapshot(index)
            if snap_state == 'SUCCESS':
                # TODO make deletion of index from cluster optional?
                self.es_client.indices.delete(index)
            else:
                self.es_client.snapshot.delete('s3', index)
            snap_states[snap_state] = index

        return snap_states


    def snapshots(self, snapshots=None):
        """
        List snapshots their state & etc.
        """
        snaps = self.es_client.snapshot.get(self.repository_name, snapshots or '_all')
        return snaps['snapshots']

    def snapshot_state(self, snapshot):
        """
        Return state of specified snapshot or 'unknown'.
        """
        try:
            for snap in self.snapshots(snapshot):
                if snap['snapshot'] == snapshot:
                    return snap['state']
        except NotFoundError:
            pass
        return 'unknown'

    def _wait_for_snapshot(self, snapshot):
        """
        Wait for specified snapshots to complete snapshotting process.
        """
        max_time = time() + SNAPSHOT_WAIT_TIMEOUT
        while True:
            sleep(SNAPSHOT_POLL_INTERVAL)
            snap_state = self.snapshot_state(snapshot)
            if snap_state in ES_SNAPSHOT_FINAL_STATES:
                return snap_state
            if time() > max_time:
                raise TimeoutError(
                    "Timed out ({0}s) waiting for {1} to enter final state."
                    .format(SNAPSHOT_WAIT_TIMEOUT, snapshot)
                )

    def restore(self, snapshot):
        """
        Bring back index from archive to ES cluster
        """
        self.es_client.snapshot.restore(self.repository_name, snapshot)

"""
Tests Socify Helper
"""
from unittest import TestCase
import requests
import requests_mock

from disco_aws_automation.socify_helper import SocifyHelper
from tests.helpers.patch_disco_aws import get_mock_config

SOCIFY_API_BASE = 'https://socify-ci.aws.wgen.net/soc'


class SocifyHelperTest(TestCase):
    """Test Socify Helper"""
    def setUp(self):
        soc_config = {
            'socify':
                {'socify_baseurl': 'https://socify-ci.aws.wgen.net/soc'}
        }
        self._soc_helper = SocifyHelper("AL-1102",
                                        False,
                                        "ExampleEvent",
                                        env="test_env",
                                        config=get_mock_config(soc_config))
        self._soc_helper.ami_id = "ami_12345"

    def test_socify_helper_constr(self):
        """Test SocifyHelper Constructor with valid data"""
        soc_config = {
            'socify':
                {'socify_baseurl': 'https://socify-ci.aws.wgen.net/soc'}
        }
        soc_helper = SocifyHelper("AL-1102",
                                  False,
                                  "ExampleEvent",
                                  env="test_env",
                                  config=get_mock_config(soc_config))
        self.assertEqual("https://socify-ci.aws.wgen.net/soc", soc_helper._socify_url)

    def test_socify_helper_constr_no_soc_config(self):
        """Test SocifyHelper Constructor when the socify section is missing from the config"""
        soc_helper = SocifyHelper("AL-1102",
                                  False,
                                  "ExampleEvent",
                                  env="test_env",
                                  config=get_mock_config({}))
        self.assertFalse(hasattr(soc_helper, '_socify_url'))

    def test_socify_helper_constr_no_soc_baseurl(self):
        """Test SocifyHelper Constructor when the socify base_url is missing from the config"""
        soc_config = {
            'socify':
                {'baseurl': 'https://socify-ci.aws.wgen.net/soc'}
        }
        soc_helper = SocifyHelper("AL-1102",
                                  False,
                                  "ExampleEvent",
                                  env="test_env",
                                  config=get_mock_config(soc_config))
        self.assertFalse(hasattr(soc_helper, '_socify_url'))

    def test_build_url(self):
        """Test socify build url"""
        url = self._soc_helper._build_url("EVENT")
        self.assertEqual(url, "https://socify-ci.aws.wgen.net/soc/event")
        url = self._soc_helper._build_url("VALIDATE")
        self.assertEqual(url, "https://socify-ci.aws.wgen.net/soc/validate")

    def test_build_json(self):
        """Test socify build event json data"""
        data = self._soc_helper._build_json(SocifyHelper.SOC_EVENT_OK, hostclass="myhostclass",
                                            msg="test was successfull")
        res_data = {"ticketId": "AL-1102",
                    "cmd": "ExampleEvent",
                    "data": {"status": SocifyHelper.SOC_EVENT_OK,
                             "amiId": "",
                             "hostclass": "myhostclass",
                             "environment": "test_env",
                             "msg": "test was successfull"}}
        self.assertEqual(data, res_data)

    def test_build_json_with_sub_command(self):
        """Test socify build event json data"""
        self._soc_helper._sub_command = 'mySubCommand'
        data = self._soc_helper._build_json(SocifyHelper.SOC_EVENT_OK, hostclass="myhostclass",
                                            msg="test was successfull")
        res_data = {"ticketId": "AL-1102",
                    "cmd": "ExampleEvent",
                    "data": {"status": SocifyHelper.SOC_EVENT_OK,
                             "amiId": "",
                             "sub_cmd": "mySubCommand",
                             "hostclass": "myhostclass",
                             "environment": "test_env",
                             "msg": "test was successfull"}}
        self.assertEqual(data, res_data)

    @requests_mock.Mocker()
    def test_send_event(self, mock_requests):
        """Test send event with no error"""
        mock_response = {
            'message': 'SOCIFY has successfully processed the event: ExampleEvent'
        }
        mock_requests.post(SOCIFY_API_BASE + "/event", json=mock_response)
        self.assertEqual(self._soc_helper.send_event(SocifyHelper.SOC_EVENT_OK,
                                                     ami_id="ami_12345",
                                                     msg="test was successfull"),
                         "SOCIFY has successfully processed the event: ExampleEvent")

    @requests_mock.Mocker()
    def test_send_event_timeout(self, mock_requests):
        """Test send event with timeout error"""
        mock_requests.post(SOCIFY_API_BASE + "/event", exc=requests.exceptions.ConnectTimeout)
        self.assertEqual(self._soc_helper.send_event(SocifyHelper.SOC_EVENT_OK,
                                                     ami_id="ami_12345",
                                                     msg="test was successfull"),
                         "Failed sending the Socify event: ")

    @requests_mock.Mocker()
    def test_send_event_httperror(self, mock_requests):
        """Test send event with error message"""
        mock_response = {
            'errorMessage': 'SOCIFY Invalid Ticket'
        }
        return_error = 'Socify event failed with the following error: SOCIFY Invalid Ticket'
        mock_requests.post(SOCIFY_API_BASE + "/event", json=mock_response, status_code=400)
        self.assertEqual(self._soc_helper.send_event(SocifyHelper.SOC_EVENT_OK,
                                                     ami_id="ami_12345",
                                                     msg="test was successfull"),
                         return_error)

    @requests_mock.Mocker()
    def test_event_httperror_no_json(self, mock_requests):
        """Test send event with error message"""
        mock_response = 'SOCIFY Invalid Ticket'

        return_error = 'Socify event failed with the following error: *'
        mock_requests.post(SOCIFY_API_BASE + "/event", json=mock_response, status_code=400)
        self.assertRegexpMatches(self._soc_helper.send_event(SocifyHelper.SOC_EVENT_OK,
                                                             ami_id="ami_12345",
                                                             msg="test was successfull"),
                                 return_error)

    @requests_mock.Mocker()
    def test_validate(self, mock_requests):
        """Test validate with no error"""
        mock_response = {
            'message': 'SOCIFY has successfully processed the validate request: ExampleEvent',
            'result': {'status': 'Passed', 'err_msgs': []}
        }
        mock_requests.post(SOCIFY_API_BASE + "/validate", json=mock_response, status_code=200)
        self.assertTrue(self._soc_helper.validate())

    @requests_mock.Mocker()
    def test_validate_failed(self, mock_requests):
        """Test validate when returned False"""
        mock_response = {
            'message': 'SOCIFY has successfully processed the validate request: ExampleEvent',
            'result': {'status': 'Failed', 'err_msgs': ["Some error message"]}
        }
        mock_requests.post(SOCIFY_API_BASE + "/validate", json=mock_response, status_code=200)
        self.assertFalse(self._soc_helper.validate())

    @requests_mock.Mocker()
    def test_validate_httperror(self, mock_requests):
        """Test send event with error message"""
        mock_response = {
            'errorMessage': 'SOCIFY failed executing the validate request'
        }
        mock_requests.post(SOCIFY_API_BASE + "/validate", json=mock_response, status_code=400)
        self.assertFalse(self._soc_helper.validate())

    @requests_mock.Mocker()
    def test_validate_httperror_no_json(self, mock_requests):
        """Test send event with error message"""
        mock_response = 'SOCIFY failed executing the validate request'
        mock_requests.post(SOCIFY_API_BASE + "/validate", json=mock_response, status_code=400)
        self.assertFalse(self._soc_helper.validate())

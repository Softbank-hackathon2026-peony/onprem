import io
import json
from unittest.mock import Mock, patch
import tunnel_server as tunnel


def handler(path, payload, token='secret'):
    request = object.__new__(tunnel.Handler)
    body = json.dumps(payload).encode()
    request.path = path
    request.headers = {'X-Tunnel-Token':token,'Content-Length':str(len(body))}
    request.rfile = io.BytesIO(body)
    request.wfile = io.BytesIO()
    request.send_error = Mock()
    request.send_response = Mock()
    request.send_header = Mock()
    request.end_headers = Mock()
    return request


def test_unknown_path_does_not_close_existing_tunnel():
    request = handler('/unknown',{})
    with patch.object(tunnel,'TOKEN','secret'), patch.object(tunnel,'cleanup') as cleanup:
        request.do_POST()
        request.send_error.assert_called_once_with(404)
        cleanup.assert_not_called()


def test_invalid_request_does_not_close_another_app():
    request = handler('/publish',{'service_name':'bad/name','namespace':'pawploy'})
    with patch.object(tunnel,'TOKEN','secret'), patch.object(tunnel,'cleanup') as cleanup:
        request.do_POST()
        cleanup.assert_not_called()
        assert request.send_error.call_args.args[0] == 502


def test_repeated_publish_returns_same_url_without_starting_processes():
    request = handler('/publish',{'service_name':'app-demo','namespace':'pawploy'})
    process = Mock(); process.poll.return_value = None
    with patch.object(tunnel,'TOKEN','secret'), patch.object(tunnel,'active',('pawploy','app-demo','https://demo.ngrok.app')), patch.object(tunnel,'processes',[process]), patch.object(tunnel.subprocess,'Popen') as start:
        request.do_POST()
        start.assert_not_called()
        assert json.loads(request.wfile.getvalue()) == {'url':'https://demo.ngrok.app'}


def test_close_only_closes_matching_app():
    request = handler('/close',{'service_name':'app-other','namespace':'pawploy'})
    with patch.object(tunnel,'TOKEN','secret'), patch.object(tunnel,'active',('pawploy','app-demo','https://demo.ngrok.app')), patch.object(tunnel,'cleanup') as cleanup:
        request.do_POST()
        cleanup.assert_not_called()


def test_publish_uses_endpoint_api_and_host_processes_only():
    request = handler('/publish',{'service_name':'app-demo','namespace':'pawploy','port':8080})
    process = Mock(); process.poll.return_value = None
    connection = Mock(); connection.__enter__ = Mock(return_value=connection); connection.__exit__ = Mock(return_value=False)
    response = io.BytesIO(b'{"endpoints":[{"url":"https://demo.ngrok.app"}]}')
    with patch.object(tunnel,'TOKEN','secret'), patch.object(tunnel,'active',None), patch.object(tunnel,'processes',[]), patch.object(tunnel,'server',Mock(ngrok_config='/tmp/ngrok.yml'),create=True), patch.dict(tunnel.os.environ,{'NGROK_AUTHTOKEN':'test-token'}), patch.object(tunnel.socket,'create_connection',return_value=connection), patch.object(tunnel,'urlopen',return_value=response) as api, patch.object(tunnel.subprocess,'Popen',return_value=process) as start:
        request.do_POST()
        assert start.call_count == 2
        assert start.call_args_list[0].args[0][-2:] == ['18080:8080','--address=127.0.0.1']
        assert start.call_args_list[1].args[0] == ['ngrok','http','http://127.0.0.1:18080','--config','/tmp/ngrok.yml']
        api.assert_called_once_with('http://127.0.0.1:4041/api/endpoints',timeout=2)
        assert json.loads(request.wfile.getvalue())['url'] == 'https://demo.ngrok.app'

import json
import logging
import yaml
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.exceptions import RequestException

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_http_port(ip_port):
    ports_to_try = []
    try:
        response = requests.get(f"http://{ip_port}/configs", timeout=4)
        data = response.json()
        if data["port"] != 0:
            ports_to_try.append(data["port"])
        if data["mixed-port"] != 0:
            ports_to_try.append(data["mixed-port"])
    except Exception as e:
        print(f"An error occurred: {e}")
    return ports_to_try


def get_ip_location(ip):
    return "未知"
    ##百度地图已失效，后续改为其他服务
    try:
        response = requests.get(f"http://api.map.baidu.com/location/ip?ak=LMfOH6zhz1dT0TLuwgG5okM5sNZB4amI&ip={ip}&coor=bd09ll")
        data = response.json()
        if data["status"] == 0:
            city = data["content"]["address_detail"]["city"]
            return city
        else:
            logging.error(f'Error: Failed to get location for {ip} with status {data["status"]}')
            return "未知"
    except RequestException as e:
        logging.error(f'Error: Could not get location for {ip} - {e}')
        return "未知"

def test_proxy(ip, port, timeout=2):
    url = 'http://www.google.com'
    proxy = {
        "http": f"http://{ip}:{port}",
        "https": f"http://{ip}:{port}"
    }
    try:
        response = requests.get(url, proxies=proxy, timeout=timeout)
        if response.status_code == 200 and 'Google' in response.text:
            logging.info(f'Success: Connected to {ip}:{port} via proxy and verified Google access')
            return True
        else:
            logging.warning(f'Failed: {ip}:{port} returned status code {response.status_code} or response content is invalid')
    except RequestException as e:
        logging.error(f'Error: Could not connect to {ip}:{port} via proxy - {e}')
    return False
def main():
    input_file = 'proxies.txt'
    output_file = 'proxies.yaml'
    # 目标目录路径
    dest_dir = '/Users/siyushi/Library/ApplicationSupport/io.github.clash-verge-rev.clash-verge-rev/'

    default_ports_to_try = [7890, 7893]
    max_threads = 50

    with open(input_file, 'r') as f:
        proxies = json.load(f)

    working_proxies = []
    city_counter = defaultdict(int)

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        port_futures = {}
        for proxy in proxies:
            ip = proxy.split(':')[0]
            logging.info(f'Scanning ports for {ip}...')
            future = executor.submit(get_http_port, proxy)
            port_futures[future] = ip

        proxy_test_futures = []
        for port_future in as_completed(port_futures):
            ip = port_futures[port_future]
            ports_to_try = port_future.result()
            if not ports_to_try:
                ports_to_try = default_ports_to_try
            for port in ports_to_try:
                logging.info(f'Testing {ip}:{port}...')
                future = executor.submit(test_proxy, ip, port)
                proxy_test_futures.append((ip, port, future))

        location_futures = {}
        for ip, port, future in proxy_test_futures:
            result = future.result()
            if result:
                loc_future = executor.submit(get_ip_location, ip)
                location_futures[loc_future] = (ip, port)

        for loc_future in as_completed(location_futures):
            ip, port = location_futures[loc_future]
            city = loc_future.result()
            city_counter[city] += 1
            node_name = f"{city}{city_counter[city]}"
            working_proxies.append({
                "name": node_name,
                "type": "http",
                "server": ip,
                "port": port
            })

    clash_config = {
        "proxies": working_proxies
    }

    with open(output_file, 'w') as f:
        yaml.dump(clash_config, f, allow_unicode=True, default_flow_style=False)
    logging.info(f'Configuration saved to {output_file}')
    #shutil.copy(output_file, dest_dir)
if __name__ == '__main__':
    main()

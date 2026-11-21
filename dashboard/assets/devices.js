(() => {
    'use strict';

    const $ = id => document.getElementById(id);
    const boot = window.WALK_BOOT;
    const selected = () => new URLSearchParams(location.hash.slice(1)).get('device') || boot.history.device_id;

    async function request(path, data) {
        const headers = {Authorization: 'Bearer ' + boot.access};
        const options = {cache: 'no-store', headers};
        if (data) {
            headers['Content-Type'] = 'application/json';
            options.method = 'POST';
            options.body = JSON.stringify(data);
        }
        const response = await fetch(path, options);
        const body = await response.json();
        if (!response.ok) throw Error(body.error || 'Request failed');
        return body;
    }

    function node(tag, text) {
        const element = document.createElement(tag);
        element.textContent = text;
        return element;
    }

    function action(text, onClick) {
        const button = node('button', text);
        button.className = 'text-button';
        button.onclick = onClick;
        return button;
    }

    function switchDevice(id) {
        const params = new URLSearchParams(location.hash.slice(1));
        if (id) params.set('device', id);
        else params.delete('device');
        params.delete('at');
        params.delete('c');
        params.delete('z');
        params.set('lo', '0');
        params.set('hi', '1000');
        history.replaceState(null, '', '#' + params.toString());
        location.reload();
    }

    function showKeyForm(device) {
        const form = $('hubbleKeyForm');
        form.reset();
        form.elements.device_id.value = device.id;
        $('hubbleKeyName').textContent = device.name + ' encryption key';
        $('hubbleKeyMessage').textContent = '';
        form.hidden = false;
        form.elements.private_key.focus();
    }

    function renderDevice(device) {
        const {id, name, provider, active, state, report_count, packet_count, key_saved} = device;
        const row = document.createElement('div');
        row.className = 'inventory-row';
        const apple = provider === 'openhaystack';
        const providerName = apple ? 'OpenHaystack' : 'Hubble';
        const packetInfo = apple ? '' : ` · ${packet_count} packets${key_saved ? ' · Key saved' : ''}`;
        const detail = `${providerName} · ${state.replaceAll('_', ' ')} · ${report_count} locations${packetInfo}`;
        row.append(node('strong', name), node('small', detail), action('View', () => switchDevice(id)));

        if (apple) {
            row.append(action('Rename', async () => {
                const next = prompt(`New name for "${name}"`, name);
                if (next === null || next.trim() === name) return;
                try {
                    await request('/api/devices/rename', {id, name: next});
                    $('inventoryMessage').textContent = `${name} is now ${next.trim()}.`;
                    await load();
                } catch (error) {
                    $('inventoryMessage').textContent = error.message;
                }
            }));
            row.append(action(active ? 'Pause collection' : 'Enable collection', async () => {
                try {
                    await request('/api/devices/update', {id, active: !active});
                    await load();
                } catch (error) {
                    $('inventoryMessage').textContent = error.message;
                }
            }));
        } else {
            row.append(action(key_saved ? 'Replace key' : 'Save key', () => showKeyForm(device)));
        }

        row.append(action('Delete', async event => {
            if (!confirm(`Delete "${name}" from this dashboard? Collection will stop for this device. Saved history and keys will be kept on this host for recovery.`)) return;
            const button = event.currentTarget;
            button.disabled = true;
            try {
                await request('/api/devices/delete', {id});
                $('inventoryMessage').textContent = name + ' was deleted from the dashboard.';
                if ($('hubbleKeyForm').elements.device_id.value === id) hideKeyForm();
                if (loading) await loading;
                await load();
            } catch (error) {
                $('inventoryMessage').textContent = error.message;
            } finally {
                button.disabled = false;
            }
        }));
        return row;
    }

    function sensorText(sensors) {
        return `${sensors.vcc_v.toFixed(3)} V · ${sensors.temp_c.toFixed(2)} °C · ${sensors.adv_count.toLocaleString()} adverts`;
    }

    let packetSignature;
    function renderPackets(packets) {
        const signature = JSON.stringify(packets);
        if (signature === packetSignature) return;
        packetSignature = signature;
        const latest = packets.find(packet => packet.sensors);
        $('latestSensors').hidden = !latest;
        if (latest) {
            $('sensorValues').textContent = sensorText(latest.sensors);
            $('sensorTime').textContent = new Date(latest.timestamp).toLocaleString();
        }
        $('packetList').replaceChildren(...packets.slice(0, 10).map(packet => {
            const row = document.createElement('details');
            const date = new Date(packet.timestamp).toLocaleString();
            const summary = node('summary', packet.sensors ? date + ' · ' + sensorText(packet.sensors) : date);
            row.append(summary, node('pre', JSON.stringify(packet.packet, null, 2)));
            return row;
        }));
        $('packetSection').hidden = !packets.length;
    }

    let loading;
    async function load() {
        if (loading) return loading;
        loading = (async () => {
            try {
                const current = selected();
                const data = await request('/api/devices');
                if (current !== data.default_device && !data.devices.some(d => d.id === current)) {
                    switchDevice(data.default_device);
                    return;
                }
                const options = data.devices.map(device => {
                    const provider = device.provider === 'hubble' ? 'Hubble' : 'OpenHaystack';
                    const option = node('option', device.name + ' · ' + provider);
                    option.value = device.id;
                    return option;
                });
                $('deviceSelect').replaceChildren(...(options.length ? options : [node('option', 'No devices')]));
                $('deviceSelect').disabled = !options.length;
                if (current) $('deviceSelect').value = current;
                $('deviceInventory').replaceChildren(...data.devices.map(renderDevice));
                const savedKeyDevice = $('hubbleKeyForm').elements.device_id.value;
                if (savedKeyDevice && !data.devices.some(d => d.id === savedKeyDevice)) hideKeyForm();
                $('connectionStatus').textContent = data.connections.length
                    ? data.connections.map(c => `Hubble ${c.org_id}: ${c.state.replaceAll('_', ' ')}${c.error ? ' (' + c.error + ')' : ''}`).join('\n')
                    : 'Enter your organization ID and a read-only API token to connect Hubble.';
                const device = data.devices.find(d => d.id === current);
                const result = device?.provider === 'hubble'
                    ? await request('/api/packets?device=' + encodeURIComponent(current))
                    : {packets: []};
                renderPackets(result.packets);
                $('deviceMessage').textContent = '';
            } catch (error) {
                $('deviceMessage').textContent = error.message;
            }
        })();
        try {
            return await loading;
        } finally {
            loading = null;
        }
    }

    function hideKeyForm() {
        const form = $('hubbleKeyForm');
        form.reset();
        form.hidden = true;
    }

    $('deviceSelect').onchange = event => switchDevice(event.target.value);
    $('addDeviceButton').onclick = () => $('deviceDialog').showModal();
    $('closeDeviceDialog').onclick = () => $('deviceDialog').close();
    $('deviceDialog').addEventListener('close', hideKeyForm);
    $('cancelHubbleKey').onclick = hideKeyForm;

    $('hubbleKeyForm').onsubmit = async event => {
        event.preventDefault();
        const form = event.currentTarget;
        const msg = $('hubbleKeyMessage');
        msg.textContent = 'Saving…';
        try {
            await request('/api/devices/hubble-key', {
                id: form.elements.device_id.value,
                private_key: form.elements.private_key.value
            });
            hideKeyForm();
            $('inventoryMessage').textContent = 'Hubble device key saved on the host.';
            await load();
        } catch (error) {
            msg.textContent = error.message;
        }
    };

    $('addOpenHaystack').onsubmit = async event => {
        event.preventDefault();
        const form = event.currentTarget;
        const msg = $('deviceFormMessage');
        msg.textContent = 'Saving…';
        try {
            const result = await request('/api/devices', {
                name: form.elements.name.value,
                private_key: form.elements.private_key.value
            });
            form.reset();
            switchDevice(result.id);
        } catch (error) {
            msg.textContent = error.message;
        }
    };

    $('connectHubble').onsubmit = async event => {
        event.preventDefault();
        const form = event.currentTarget;
        const msg = $('hubbleFormMessage');
        msg.textContent = 'Saving…';
        try {
            await request('/api/connections/hubble', {
                org_id: form.elements.org_id.value,
                token: form.elements.token.value
            });
            form.elements.token.value = '';
            msg.textContent = 'Saved on the host. Check the connection status below.';
            await load();
        } catch (error) {
            msg.textContent = error.message;
        }
    };

    load();
    setInterval(load, 15000);
})();

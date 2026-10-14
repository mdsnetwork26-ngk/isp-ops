/* ISP-OPS - antarmuka teknisi (vanilla JS, tanpa library) */
'use strict';

// ------------------------------------------------------------------ util
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== '') node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === '') continue;
    node.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, body) {
  const opt = body === undefined
    ? { method: 'GET' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  let data = {};
  try { data = await res.json(); } catch (e) { /* bukan JSON */ }
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, kind = '') {
  const box = $('#toast');
  if (!box) return;
  const div = el('div', { class: kind }, msg);
  box.appendChild(div);
  setTimeout(() => div.remove(), kind === 'err' ? 7000 : 3500);
}

const STATUS_LABEL = { up: 'UP', down: 'DOWN', isolated: 'ISOLIR', disabled: 'DISABLED', 'no-secret': 'TANPA SECRET' };

function waktu(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (isNaN(d)) return '-';
  return d.toLocaleString('id-ID', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function bukaModal(id) { $(id).classList.add('buka'); }
// auto-isolir: masa tenggang (hari) dikirim server; default 2
let GRACE_DAYS = 2;

function tanggalEfektif(c) {
  if (!c.auto_isolir_date) return '';
  const d = new Date(c.auto_isolir_date + 'T00:00:00');
  d.setDate(d.getDate() + GRACE_DAYS);
  return d.toLocaleDateString('id-ID');
}

function autoIsolirEfektif(c) {
  if (!c.auto_isolir) return false;
  if (!c.auto_isolir_date) return true;
  const d = new Date(c.auto_isolir_date + 'T00:00:00');
  d.setDate(d.getDate() + GRACE_DAYS);
  return new Date() >= d;
}
function tutupModal(id) { $(id).classList.remove('buka'); }
document.addEventListener('click', (e) => {
  if (e.target.matches('[data-tutup]')) e.target.closest('.modal').classList.remove('buka');
  if (e.target.classList.contains('modal')) e.target.classList.remove('buka');
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $$('.modal.buka').forEach((m) => m.classList.remove('buka'));
});

// ================================================================ MONITORING
if (document.getElementById('tabel')) {
  const state = { role: 'admin', routers: [], filter: 'all', q: '', routerId: '', limit: 100, data: null, timer: null };

  function statusBadge(status) {
    return el('span', { class: `badge ${status}` }, STATUS_LABEL[status] || status.toUpperCase());
  }

  function bayarBadge(s) {
    if (s.is_paid === true) return el('span', { class: 'badge bayar-yes' }, 'Lunas');
    if (s.is_paid === false) return el('span', { class: 'badge bayar-no' }, 'Belum bayar');
    return el('span', { class: 'muted' }, '-');
  }

  function aksiCell(s, routerId) {
    const wrap = el('div', { class: 'aksi' });
    const ident = { id: s.customer_id, name: s.name, router_id: routerId };

    wrap.appendChild(el('button', {
      class: 'btn mini',
      title: 'Lihat detail, ping, dan remote ONU',
      onclick: () => detail(s, routerId),
    }, 'Detail'));

    if (state.role !== 'pemantau') {
      if (s.status === 'isolated') {
        wrap.appendChild(el('button', {
          class: 'btn mini ok',
          onclick: () => doBuka(ident, s.name),
        }, 'Buka isolir'));
      } else {
        wrap.appendChild(el('button', {
          class: 'btn mini danger',
          onclick: () => konfirmasiIsolir(ident, s.name),
        }, 'Isolir'));
      }
    }

    wrap.appendChild(el('button', {
      class: 'btn mini',
      title: s.ip ? `Buka web ONU ${s.ip}` : 'IP pelanggan belum diketahui',
      onclick: () => remoteOnu(s, routerId),
    }, 'Remote ONU'));

    return wrap;
  }

  function renderCards(routers) {
    const box = $('#routerCards');
    box.innerHTML = '';
    if (!routers.length) {
      box.appendChild(el('div', { class: 'muted' }, 'Belum ada router. Tambahkan di menu Pengaturan.'));
      return;
    }
    for (const r of routers) {
      const st = r.stats || {};
      box.appendChild(el('div', { class: 'card' },
        el('h4', {},
          el('span', { class: `dot ${r.online ? 'up' : 'bad'}` }),
          r.router_name,
          el('span', { class: 'muted' }, ` ${r.online ? r.ms + ' ms' : 'offline'}`),
        ),
        el('div', { class: 'muted', style: 'margin-bottom:8px' }, r.host),
        el('div', { class: 'stat' },
          el('div', {}, el('b', {}, String(st.up ?? 0)), 'online'),
          el('div', {}, el('b', {}, String(st.down ?? 0)), 'offline'),
          el('div', {}, el('b', {}, String(st.isolated ?? 0)), 'isolir'),
          el('div', {}, el('b', {}, String(st.total ?? 0)), 'total'),
        ),
        r.online ? null : el('div', { class: 'muted', style: 'margin-top:8px;color:#fca5a5' }, r.message),
      ));
    }
  }

  function renderTable(data) {
    const tbody = $('#tbody');
    tbody.innerHTML = '';
    const rows = [];
    for (const r of (data.routers || [])) {
      for (const s of (r.sessions || [])) rows.push({ s, routerId: r.router_id, routerName: r.router_name });
    }
    if (!rows.length) {
      tbody.appendChild(el('tr', {}, el('td', { class: 'kosong', colspan: 6 },
        data.routers && data.routers.length ? 'Tidak ada data yang cocok dengan filter.' : 'Belum ada data. Periksa koneksi ke mikrotik atau tambahkan router di Pengaturan.')));
      return;
    }
    for (const { s, routerId } of rows) {
      const tr = el('tr', { class: s.status === 'isolated' ? 'baris-isolir' : (s.status === 'down' ? 'baris-down' : '') });
      tr.appendChild(el('td', {}, el('div', { class: 'nama' }, s.name,
        s.full_name ? el('small', {}, s.full_name + (s.phone ? ' · ' + s.phone : '')) : null,
        s.address ? el('small', {}, s.address) : null)));
      tr.appendChild(el('td', { class: 'mono' }, s.ip || '-'));
      tr.appendChild(el('td', { class: 'mono col-iface' }, s.caller_id || '-'));
      tr.appendChild(el('td', { class: 'uptime col-uptime', title: s.uptime_seconds + ' detik' }, s.uptime));
      tr.appendChild(el('td', {}, statusBadge(s.status),
        s.disabled && s.status !== 'isolated' ? el('span', { class: 'badge disabled', style: 'margin-left:4px' }, 'SECRET OFF') : null,
        s.auto_isolir ? el('span', {
          class: `badge ${autoIsolirEfektif(s) ? 'isolated' : 'down'}`, style: 'margin-left:4px',
          title: autoIsolirEfektif(s) ? 'Diisolir otomatis saat online' : `Auto-isolir terjadwal, aktif ${tanggalEfektif(s)}`,
        }, autoIsolirEfektif(s) ? 'AUTO' : 'TERJADWAL') : null));
      tr.appendChild(el('td', {}, aksiCell(s, routerId)));
      tbody.appendChild(tr);
    }
  }

  function renderInfo(data) {
    const routers = data.routers || [];
    const total = routers.reduce((n, r) => n + (r.stats.total || 0), 0);
    const shown = routers.reduce((n, r) => n + (r.sessions || []).length, 0);
    const cocok = routers.reduce((n, r) => n + (r.matched || (r.sessions || []).length), 0);
    const ages = routers.map((r) => r.updated_at).filter(Boolean);
    const terbaru = ages.length ? Math.max(...ages) : 0;
    const detik = terbaru ? Math.max(0, Math.round(Date.now() / 1000 - terbaru)) : null;
    const potong = cocok > shown ? ` (ditampilkan ${shown}, sisanya pakai pencarian atau pilih "Semua")` : '';
    $('#infoUpdate').textContent = `${cocok === total ? 'Menampilkan' : 'Cocok'} ${cocok} dari ${total} pelanggan${potong} · data terakhir ${detik === null ? '-' : detik + ' detik lalu'}`;
    const b = data.billing || {};
    $('#infoBilling').textContent = b.enabled
      ? `Integrasi tagihan aktif (${b.cached} pelanggan menunggak di-cache)`
      : 'Status bayar manual (integrasi tagihan nonaktif — aktifkan lewat .env)';
  }

  async function muat(quiet = false) {
    const params = new URLSearchParams();
    if (state.routerId) params.set('router_id', state.routerId);
    if (state.q) params.set('q', state.q);
    if (state.filter !== 'all') params.set('status', state.filter);
    if (state.limit) params.set('limit', String(state.limit));
    try {
      const data = await api('/api/status?' + params.toString());
      if (data.auto_isolir_grace_days) GRACE_DAYS = data.auto_isolir_grace_days;
      state.data = data;
      renderCards(data.routers || []);
      const sedangKetik = document.activeElement === $('#cari');
      if (!sedangKetik) renderTable(data);
      renderInfo(data);
      isiPilihRouter(data.routers || []);
    } catch (e) {
      if (!quiet) toast('Gagal ambil data: ' + e.message, 'err');
      $('#tbody').innerHTML = '';
      $('#tbody').appendChild(el('tr', {}, el('td', { class: 'kosong', colspan: 6 }, 'Gagal mengambil data: ' + e.message)));
    }
  }

  function isiPilihRouter(routers) {
    const sel = $('#pilihRouter');
    const nilai = state.routerId;
    sel.innerHTML = '';
    sel.appendChild(el('option', { value: '' }, 'Semua router'));
    for (const r of routers) {
      sel.appendChild(el('option', { value: String(r.router_id), selected: String(r.router_id) === nilai ? 'selected' : null },
        `${r.router_name}${r.online ? '' : ' (offline)'}`));
    }
    sel.value = nilai;
  }

  function aturTimer() {
    if (state.timer) clearInterval(state.timer);
    if (!$('#autoRefresh').checked) return;
    const detik = Math.max(2, parseInt($('#interval').value, 10) || 5);
    state.timer = setInterval(() => muat(true), detik * 1000);
  }

  // ---------------------------------------------------------- aksi
  function konfirmasiIsolir(ident, nama) {
    $('#kJudul').textContent = 'Isolir pelanggan';
    $('#kTeks').textContent = `Putus akses internet untuk "${nama}"? Pelanggan tetap bisa menghubungi Anda, tapi tidak bisa browsing sampai tombol Buka Isolir diklik.`;
    $('#kAlasan').value = 'belum bayar';
    $('#kOk').textContent = 'Ya, isolir';
    $('#kOk').className = 'btn danger';
    $('#kOk').onclick = async () => {
      tutupModal('#modalKonfirmasi');
      await doIsolir(ident, $('#kAlasan').value);
    };
    bukaModal('#modalKonfirmasi');
  }

  async function doIsolir(ident, alasan) {
    try {
      const res = await api('/api/isolir', { ...ident, reason: alasan });
      toast(`Isolir berhasil: ${res.customer} (${res.ip})`, 'ok');
      await muat(true);
    } catch (e) {
      toast('Isolir gagal: ' + e.message, 'err');
    }
  }

  async function doBuka(ident, nama) {
    try {
      const res = await api('/api/buka', ident);
      toast(`Isolir dibuka: ${res.customer}. Pelanggan biasanya konek lagi dalam 10-60 detik.`, 'ok');
      await muat(true);
    } catch (e) {
      toast('Buka isolir gagal: ' + e.message, 'err');
    }
  }

  // ---------------------------------------------------------- detail
  async function detail(s, routerId) {
    $('#dJudul').textContent = s.name;
    const box = $('#dIsi');
    box.innerHTML = '';
    const baris = (label, value, mono = true) => el('div', { class: 'scan-row' },
      el('div', { class: 'info', style: 'max-width:170px' }, el('span', { class: 'muted' }, label)),
      el('div', { class: mono ? 'mono' : '' }, value || '-'));
    box.appendChild(baris('Status', STATUS_LABEL[s.status] || s.status, false));
    box.appendChild(baris('Nama pelanggan', `${s.full_name || '-'} ${s.phone ? '(' + s.phone + ')' : ''}`, false));
    box.appendChild(baris('Alamat', s.address, false));
    box.appendChild(baris('IP PPPoE', s.ip));
    box.appendChild(baris('MAC', s.mac));
    box.appendChild(baris('Uptime', `${s.uptime} (${s.uptime_seconds} detik)`));
    box.appendChild(baris('Interface / caller', s.caller_id));
    box.appendChild(baris('IP router', s.router_ip ? `${s.router_ip}:${s.router_port}` : '-'));
        box.appendChild(baris('Catatan', s.notes, false));

    const tombol = el('div', { class: 'row', style: 'margin-top:12px' });
    tombol.appendChild(el('button', { class: 'btn mini', onclick: () => ping(s, routerId) }, 'Ping dari mikrotik'));
    tombol.appendChild(el('button', { class: 'btn mini', onclick: () => remoteOnu(s, routerId) }, 'Remote ONU'));
    if (s.ip) {
      tombol.appendChild(el('button', { class: 'btn mini', onclick: () => window.open(`/proxy/${routerId}/http/${s.ip}:${s.onu_port || 80}/`, '_blank') }, 'ONU via server'));
    }
    if (state.role === 'admin' && s.customer_id) {
      tombol.appendChild(el('button', {
        class: 'btn mini danger',
        title: 'Hapus data pelanggan dari aplikasi (secret PPPoE di mikrotik tetap ada)',
        onclick: async () => {
          if (!confirm(`Hapus data pelanggan "${s.name}" dari aplikasi? (PPP secret di mikrotik tidak dihapus)`)) return;
          try {
            await api(`/api/customers/${s.customer_id}/delete`, {});
            toast('Data pelanggan dihapus', 'ok');
            tutupModal('#modalDetail');
            muat(true);
          } catch (e) { toast('Gagal: ' + e.message, 'err'); }
        },
      }, 'Hapus dari aplikasi'));
    }
    box.appendChild(tombol);
    bukaModal('#modalDetail');
  }

  function remoteOnu(s, routerId) {
    // pelanggan terbaca dapat IP 10.1.4.39 -> remote membuka 10.1.4.39:80
    if (!s.ip) return toast('IP pelanggan belum diketahui (pelanggan offline?)', 'err');
    if (location.protocol === 'https:') {
      // diakses lewat domain (HTTPS): buka ONU lewat subdomain wildcard
      // (onu-10-20-20-3.DOMAIN) supaya halaman ONU berperilaku alami
      const parts = location.hostname.split('.');
      const twoLevelTlds = ['my', 'co', 'web', 'net', 'ac', 'sch', 'go', 'or', 'biz'];
      let domain = location.hostname;
      if (parts.length >= 3 && twoLevelTlds.includes(parts[parts.length - 2]) && parts[parts.length - 1].length === 2) {
        domain = parts.slice(-3).join('.');
      } else if (parts.length >= 2) {
        domain = parts.slice(-2).join('.');
      }
      const port = s.onu_port || 80;
      const host = 'onu-' + s.ip.split('.').join('-') + (port !== 80 ? '-' + port : '') + '.' + domain;
      window.open(`https://${host}/`, '_blank');
      return;
    }
    window.open(`http://${s.ip}:${s.onu_port || 80}/`, '_blank');
  }

  async function ping(s, routerId) {
    try {
      const res = await api('/api/ping', { id: s.customer_id, name: s.name, router_id: routerId });
      toast(res.ok ? `Ping ${res.ip} balas, ${res.ms} ms` : `Ping ${res.ip || ''} tidak balas`, res.ok ? 'ok' : 'err');
    } catch (e) { toast('Ping gagal: ' + e.message, 'err'); }
  }

  // ---------------------------------------------------------- wiring
  $('#btnRefresh').addEventListener('click', () => muat());
  $('#pilihRouter').addEventListener('change', (e) => { state.routerId = e.target.value; muat(true); });
  $('#autoRefresh').addEventListener('change', aturTimer);
  $('#interval').addEventListener('change', aturTimer);
  $('#limit').addEventListener('change', (e) => { state.limit = parseInt(e.target.value, 10) || 0; muat(true); });
  let tCari = null;
  $('#cari').addEventListener('input', (e) => {
    state.q = e.target.value;
    clearTimeout(tCari);
    tCari = setTimeout(() => muat(true), 300);
  });
  $$('.tab').forEach((tab) => tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.remove('aktif'));
    tab.classList.add('aktif');
    state.filter = tab.dataset.filter;
    muat(true);
  }));

  api('/api/me').then((m) => {
    state.role = m.role || 'admin';
    if (state.role === 'pemantau') {
      const bp = document.getElementById('btnPppoe');
      if (bp) bp.style.display = 'none';
    }
    muat(true);
  }).catch(() => {});
  muat();
  aturTimer();
  setInterval(() => { if (state.data) renderInfo(state.data); }, 5000);
}

// ================================================================ PENGATURAN
if (document.getElementById('tbodyRouter')) {
  let routers = [];

  async function muatRouter() {
    try {
      const data = await api('/api/routers');
      routers = data.routers || [];
      const tbody = $('#tbodyRouter');
      tbody.innerHTML = '';
      if (!routers.length) {
        tbody.appendChild(el('tr', {}, el('td', { class: 'kosong', colspan: 7 }, 'Belum ada router. Klik "+ Tambah router".')));
      }
      for (const r of routers) {
        const st = r.online === true ? 'terhubung' : (r.online === false ? 'gagal' : 'belum dipantau');
        tbody.appendChild(el('tr', {},
          el('td', {}, el('div', { class: 'nama' }, r.name, r.site ? el('small', {}, r.site) : null)),
          el('td', { class: 'mono' }, `${r.host}:${r.port}`),
          el('td', {}, r.username),
          el('td', {}, r.site || '-'),
          el('td', {}, r.poll_interval + ' dtk'),
          el('td', {}, el('span', { class: `badge ${r.online === true ? 'up' : (r.online === false ? 'isolated' : 'down')}` }, st),
            el('div', { class: 'muted' }, (r.message || '').slice(0, 70))),
          el('td', {}, el('div', { class: 'aksi' },
            el('button', { class: 'btn mini', onclick: () => tesRouter(r.id) }, 'Tes'),
            el('button', { class: 'btn mini', onclick: () => editRouter(r) }, 'Ubah'),
            el('button', { class: 'btn mini danger', onclick: () => hapusRouter(r) }, 'Hapus'),
          )),
        ));
      }
      // isi dropdown
      for (const id of ['filterRouter', 'cRouter']) {
        const sel = $('#' + id);
        const lama = sel.value;
        sel.innerHTML = id === 'filterRouter' ? '' : '';
        if (id === 'filterRouter') sel.appendChild(el('option', { value: '' }, 'Semua router'));
        for (const r of routers) sel.appendChild(el('option', { value: String(r.id) }, r.name));
        sel.value = lama;
      }
    } catch (e) { toast('Gagal memuat router: ' + e.message, 'err'); }
  }

  async function muatCust() {
    try {
      const rid = $('#filterRouter').value;
      let urlC = '/api/customers?source=manual'; if (rid) urlC += '&router_id=' + rid;
      const data = await api(urlC);
      const tbody = $('#tbodyCust');
      tbody.innerHTML = '';
      const rows = data.customers || [];
      if (!rows.length) {
        tbody.appendChild(el('tr', {}, el('td', { class: 'kosong', colspan: 8 }, 'Belum ada data pelanggan. Klik "Sinkron dari mikrotik" untuk menarik otomatis.')));
        return;
      }
      for (const c of rows) {
        tbody.appendChild(el('tr', {},
          el('td', { class: 'mono' }, c.name),
          el('td', {}, c.full_name || '-', c.isolated ? el('span', { class: 'badge isolated', style: 'margin-left:6px' }, 'ISOLIR') : null),
          el('td', {}, c.phone || '-'),
          el('td', { class: 'mono' }, c.ip || '-'),
          el('td', { class: 'mono' }, c.mac || '-'),
          el('td', { class: 'mono' }, c.router_ip ? `${c.router_ip}:${c.router_port}` : '-'),
          el('td', { class: 'mono' }, c.billing_code || '-'),
          el('td', {}, el('div', { class: 'aksi' },
            el('button', { class: 'btn mini', onclick: () => editCust(c) }, 'Ubah'),
            el('button', { class: 'btn mini danger', onclick: () => hapusCust(c) }, 'Hapus'),
          )),
        ));
      }
    } catch (e) { toast('Gagal memuat pelanggan: ' + e.message, 'err'); }
  }

  function editRouter(r) {
    $('#rJudul').textContent = r && r.id ? 'Ubah router' : 'Tambah router';
    $('#rId').value = r && r.id ? r.id : '';
    $('#rNama').value = r ? r.name : '';
    $('#rHost').value = r ? r.host : '';
    $('#rPort').value = r ? r.port : 8728;
    $('#rInterval').value = r ? r.poll_interval : 5;
    $('#rUser').value = r ? r.username : '';
    $('#rPass').value = '';
    $('#rManage').value = r ? r.manage_ip : '';
    $('#rSsl').value = r && r.api_ssl ? r.api_ssl : 'auto';
    $('#rSite').value = r ? r.site : '';
    $('#rEnabled').checked = r ? r.enabled : true;
    bukaModal('#modalRouter');
  }

  async function tesRouter(id) {
    toast('Menguji koneksi...');
    try {
      const res = await api(`/api/routers/${id}/test`, {});
      const info = res.info || {};
      toast(res.ok
        ? `Terhubung. ${res.message}${info.secrets !== undefined ? ` · ${info.secrets} secret, ${info.active} sesi aktif` : ''}${info.rule ? ` · rule isolir: ${info.rule}` : ''}`
        : `Gagal: ${res.message}`, res.ok ? 'ok' : 'err');
      muatRouter();
    } catch (e) { toast('Tes gagal: ' + e.message, 'err'); }
  }

  async function hapusRouter(r) {
    if (!confirm(`Hapus router "${r.name}" dan semua data pelanggannya?`)) return;
    try {
      await api(`/api/routers/${r.id}/delete`, {});
      toast('Router dihapus', 'ok');
      muatRouter();
    } catch (e) { toast('Gagal: ' + e.message, 'err'); }
  }

  function editCust(c) {
    $('#cJudul').textContent = c && c.id ? 'Ubah pelanggan' : 'Tambah pelanggan';
    $('#cId').value = c && c.id ? c.id : '';
    $('#cRouter').value = c ? String(c.router_id) : ($('#filterRouter').value || (routers[0] ? String(routers[0].id) : ''));
    $('#cNama').value = c ? c.name : '';
    $('#cFull').value = c ? c.full_name : '';
    $('#cPhone').value = c ? c.phone : '';
    $('#cBilling').value = c ? c.billing_code : '';
    $('#cAddress').value = c ? c.address : '';
    $('#cOnuPort').value = c ? c.onu_port : 80;
    $('#cRouterIp').value = c ? c.router_ip : '';
    $('#cRouterPort').value = c ? c.router_port : 80;
    $('#cNotes').value = c ? c.notes : '';
    bukaModal('#modalCust');
  }

  async function hapusCust(c) {
    if (!confirm(`Hapus data pelanggan "${c.name}" dari aplikasi ini? (PPP secret di mikrotik tidak dihapus)`)) return;
    try {
      await api(`/api/customers/${c.id}/delete`, {});
      toast('Data pelanggan dihapus', 'ok');
      muatCust();
    } catch (e) { toast('Gagal: ' + e.message, 'err'); }
  }

  $('#btnRouterBaru').addEventListener('click', () => editRouter(null));
  $('#btnCustBaru').addEventListener('click', () => bukaPppoe());
  $('#filterRouter').addEventListener('change', muatCust);

  $('#rSimpan').addEventListener('click', async () => {
    const payload = {
      id: $('#rId').value ? parseInt($('#rId').value, 10) : null,
      name: $('#rNama').value.trim(),
      host: $('#rHost').value.trim(),
      port: parseInt($('#rPort').value, 10) || 8728,
      username: $('#rUser').value.trim(),
      password: $('#rPass').value,
      site: $('#rSite').value.trim(),
      enabled: $('#rEnabled').checked,
      poll_interval: parseInt($('#rInterval').value, 10) || 5,
      manage_ip: $('#rManage').value.trim(),
      api_ssl: $('#rSsl').value,
    };
    if (!payload.name || !payload.host || !payload.username) return toast('Nama, host, dan username wajib diisi', 'err');
    try {
      await api('/api/routers', payload);
      toast('Router disimpan', 'ok');
      tutupModal('#modalRouter');
      muatRouter();
    } catch (e) { toast('Gagal menyimpan: ' + e.message, 'err'); }
  });

  $('#cSimpan').addEventListener('click', async () => {
    const payload = {
      id: $('#cId').value ? parseInt($('#cId').value, 10) : null,
      router_id: parseInt($('#cRouter').value, 10),
      name: $('#cNama').value.trim(),
      full_name: $('#cFull').value.trim(),
      phone: $('#cPhone').value.trim(),
      billing_code: $('#cBilling').value.trim(),
      address: $('#cAddress').value.trim(),
      ip: '',
      mac: '',
      onu_port: parseInt($('#cOnuPort').value, 10) || 80,
      router_ip: $('#cRouterIp').value.trim(),
      router_port: parseInt($('#cRouterPort').value, 10) || 80,
      notes: $('#cNotes').value.trim(),
    };
    if (!payload.name || !payload.router_id) return toast('Nama PPPoE dan router wajib diisi', 'err');
    try {
      await api('/api/customers', payload);
      toast('Pelanggan disimpan', 'ok');
      tutupModal('#modalCust');
      muatCust();
    } catch (e) { toast('Gagal menyimpan: ' + e.message, 'err'); }
  });

  $('#btnSync').addEventListener('click', async () => {
    const rid = $('#filterRouter').value || (routers[0] ? routers[0].id : '');
    if (!rid) return toast('Pilih router dulu', 'err');
    try {
      const res = await api(`/api/customers/sync?router_id=${rid}`, {});
      toast(`Sinkron selesai: ${res.added} baru, ${res.updated} diperbarui dari ${res.total_secret} secret`, 'ok');
      muatCust();
    } catch (e) { toast('Sinkron gagal: ' + e.message, 'err'); }
  });

  // ---------------------------------------------------------- auto-isolir (admin)
  async function muatAutoIsolir() {
    const q = $('#aiCari').value.trim();
    const tbody = $('#tbodyAuto');
    if (!q) {
      tbody.innerHTML = '';
      tbody.appendChild(el('tr', {}, el('td', { class: 'kosong', colspan: 5 }, 'Ketik nama PPPoE lalu klik Cari.')));
      return;
    }
    try {
      const data = await api('/api/customers?q=' + encodeURIComponent(q));
      const rows = (data.customers || []).slice(0, 50);
      tbody.innerHTML = '';
      if (!rows.length) {
        tbody.appendChild(el('tr', {}, el('td', { class: 'kosong', colspan: 5 }, 'Tidak ditemukan.')));
        return;
      }
      const namaRouter = (rid) => { const r = routers.find((x) => x.id === rid); return r ? r.name : rid; };
      GRACE_DAYS = data.auto_isolir_grace_days || 2;
      for (const c of rows) {
        const tanggal = el('input', { type: 'date', value: c.auto_isolir_date || '', style: 'width:150px' });
        const efektif = autoIsolirEfektif(c);
        const badge = !c.auto_isolir
          ? el('span', { class: 'badge up' }, 'manual')
          : (efektif
            ? el('span', { class: 'badge isolated', title: 'Sedang berlaku: diisolir tiap online' }, 'AUTO')
            : el('span', { class: 'badge down', title: `Mulai isolir ${tanggalEfektif(c)}` }, 'terjadwal'));
        tbody.appendChild(el('tr', {},
          el('td', { class: 'mono' }, c.name),
          el('td', {}, c.full_name || '-'),
          el('td', {}, String(namaRouter(c.router_id))),
          el('td', {}, tanggal, ' ', badge),
          el('td', {}, el('div', { class: 'aksi kanan' },
            el('button', {
              class: `btn mini ${c.auto_isolir ? '' : 'danger'}`,
              onclick: () => toggleAutoIsolir(c, tanggal.value),
            }, c.auto_isolir ? 'Simpan / Nonaktifkan' : 'Aktifkan auto-isolir'))),
        ));
      }
    } catch (e) { toast('Gagal mencari: ' + e.message, 'err'); }
  }

  async function toggleAutoIsolir(c, tanggal) {
    if (!c.auto_isolir) {
      // aktifkan (pakai tanggal bila diisi)
      const ket = tanggal
        ? `Isolir mulai aktif ${GRACE_DAYS} hari setelah ${tanggal}.`
        : 'Tanpa tanggal: langsung diisolir setiap kali online.';
      if (!confirm(`Aktifkan auto-isolir untuk "${c.name}"? ${ket}`)) return;
      try {
        await api(`/api/customers/${c.id}/auto-isolir`, { enabled: true, date: tanggal || '' });
        toast('Auto-isolir aktif', 'ok');
        muatAutoIsolir();
      } catch (e) { toast('Gagal: ' + e.message, 'err'); }
      return;
    }
    // sudah aktif: tanggal berubah -> simpan tanggal; tidak berubah -> konfirmasi nonaktif
    if ((tanggal || '') !== (c.auto_isolir_date || '')) {
      if (!confirm(`Simpan tanggal ${tanggal || '(kosong)'} untuk "${c.name}"?`)) return;
      try {
        await api(`/api/customers/${c.id}/auto-isolir`, { enabled: true, date: tanggal || '' });
        toast('Tanggal disimpan', 'ok');
        muatAutoIsolir();
      } catch (e) { toast('Gagal: ' + e.message, 'err'); }
      return;
    }
    if (!confirm(`Nonaktifkan auto-isolir untuk "${c.name}"? (status isolir saat ini tidak berubah)`)) return;
    try {
      await api(`/api/customers/${c.id}/auto-isolir`, { enabled: false, date: tanggal || '' });
      toast('Auto-isolir nonaktif', 'ok');
      muatAutoIsolir();
    } catch (e) { toast('Gagal: ' + e.message, 'err'); }
  }

  $('#aiCariBtn').addEventListener('click', muatAutoIsolir);
  $('#aiCari').addEventListener('keydown', (e) => { if (e.key === 'Enter') muatAutoIsolir(); });

  muatRouter();
  muatCust();
  window._segarkanCust = () => muatCust();
}

// ================================================================ LOG
if (document.getElementById('tbodyLog')) {
  async function muatLog() {
    try {
      const data = await api('/api/logs?limit=200');
      const tbody = $('#tbodyLog');
      tbody.innerHTML = '';
      if (!(data.logs || []).length) {
        tbody.appendChild(el('tr', {}, el('td', { class: 'kosong', colspan: 6 }, 'Belum ada aktivitas.')));
        return;
      }
      for (const l of data.logs) {
        tbody.appendChild(el('tr', {},
          el('td', {}, waktu(l.ts)),
          el('td', {}, l.actor),
          el('td', {}, el('span', { class: `badge ${l.success ? 'up' : 'isolated'}` }, l.action)),
          el('td', { class: 'mono' }, l.customer_name || '-'),
          el('td', {}, l.router_name || '-'),
          el('td', { class: 'muted' }, l.detail),
        ));
      }
    } catch (e) { toast('Gagal memuat log: ' + e.message, 'err'); }
  }
  $('#btnLogRefresh').addEventListener('click', muatLog);
  $('#btnLogClear').addEventListener('click', async () => {
    if (!confirm('Hapus semua log aktivitas?')) return;
    try {
      const res = await api('/api/logs/clear', {});
      toast(`${res.deleted} log dihapus`, 'ok');
      muatLog();
    } catch (e) { toast('Gagal: ' + e.message, 'err'); }
  });
  muatLog();
}

// ================================================================ PPPoE BARU (semua halaman)
  // ---------------------------------------------------------- tambah pppoe
async function muatProfile() {
  const rid = $('#pRouter').value;
  const sel = $('#pProfile');
  sel.innerHTML = '';
  if (!rid) { sel.appendChild(el('option', { value: 'default' }, 'default')); return; }
  try {
    const d = await api('/api/ppp/profiles?router_id=' + rid);
    const nama = (d.profiles && d.profiles.length) ? d.profiles : ['default'];
    for (const n of nama) sel.appendChild(el('option', { value: n }, n));
  } catch (e) {
    sel.appendChild(el('option', { value: 'default' }, 'default'));
    toast('Gagal ambil profile: ' + e.message, 'err');
  }
}

async function bukaPppoe() {
  let rs = (typeof state !== 'undefined' && state.data && state.data.routers) ? state.data.routers : null;
  if (!rs || !rs.length) {
    try { rs = (await api('/api/routers')).routers || []; } catch (e) { rs = []; }
  }
  if (!rs.length) return toast('Belum ada router tersimpan', 'err');
  const sel = $('#pRouter');
  sel.innerHTML = '';
  for (const r of rs) sel.appendChild(el('option', { value: String(r.id) }, r.name));
  $('#pNama').value = '';
  $('#pPass').value = '';
  await muatProfile();
  bukaModal('#modalPppoe');
}

async function simpanPppoe() {
  const payload = {
    router_id: parseInt($('#pRouter').value, 10),
    name: $('#pNama').value.trim(),
    password: $('#pPass').value,
    profile: $('#pProfile').value,
  };
  if (!payload.name || !payload.password) return toast('Nama dan password wajib diisi', 'err');
  try {
    const res = await api('/api/ppp/secret/add', payload);
    toast(`PPPoE ${res.name} dibuat di mikrotik (profile ${payload.profile})`, 'ok');
    tutupModal('#modalPppoe');
    if (window._segarkanMonitor) window._segarkanMonitor();
    if (window._segarkanCust) window._segarkanCust();
  } catch (e) { toast('Gagal: ' + e.message, 'err'); }
}


if (document.getElementById('modalPppoe')) {
  const bp = document.getElementById('btnPppoe');
  if (bp) bp.addEventListener('click', bukaPppoe);
  document.getElementById('pRouter').addEventListener('change', muatProfile);
  document.getElementById('pSimpan').addEventListener('click', simpanPppoe);
}

// ================================================================ logout (semua halaman)
(function () {
  const bl = document.getElementById('btnLogout');
  if (!bl) return;
  bl.addEventListener('click', async () => {
    await fetch('/api/logout', { method: 'POST' });
    window.location.href = '/login';
  });
})();

// ================================================================ pengguna (halaman pengaturan)
async function muatUser() {
  try {
    const d = await api('/api/users');
    const tbody = document.getElementById('tbodyUser');
    if (!tbody) return;
    tbody.innerHTML = '';
    for (const u of d.users || []) {
      tbody.appendChild(el('tr', {},
        el('td', { class: 'mono' }, u.username),
        el('td', {}, el('span', { class: `badge ${u.role === 'admin' ? 'isolated' : (u.role === 'teknisi' ? 'up' : 'down')}` }, u.role)),
        el('td', {}, el('div', { class: 'aksi kanan' },
          u.role === 'pemantau' ? el('button', { class: 'btn mini', onclick: () => bukaUserRouter(u) }, 'Router') : null,
          el('button', { class: 'btn mini danger', onclick: () => hapusUser(u) }, 'Hapus'))),
      ));
    }
  } catch (e) { toast('Gagal memuat pengguna: ' + e.message, 'err'); }
}

let urUserId = null;

async function bukaUserRouter(u) {
  try {
    const [dR, dU] = await Promise.all([api('/api/routers'), api(`/api/users/${u.id}/routers`)]);
    urUserId = u.id;
    $('#urJudul').textContent = `Router yang boleh dilihat: ${u.username}`;
    const wrap = $('#urDaftar');
    wrap.innerHTML = '';
    for (const r of dR.routers || []) {
      const cb = el('input', { type: 'checkbox', value: String(r.id), style: 'width:auto' });
      if ((dU.router_ids || []).includes(r.id)) cb.checked = true;
      wrap.appendChild(el('label', { style: 'display:flex;gap:8px;align-items:center;margin:6px 0' }, cb, ` ${r.name} (${r.host})`));
    }
    bukaModal('#modalUserRouter');
  } catch (e) { toast('Gagal memuat: ' + e.message, 'err'); }
}

async function hapusUser(u) {
  if (!confirm(`Hapus akun "${u.username}"?`)) return;
  try {
    await api(`/api/users/${u.id}/delete`, {});
    toast('Akun dihapus', 'ok');
    muatUser();
  } catch (e) { toast('Gagal: ' + e.message, 'err'); }
}

if (document.getElementById('tbodyUser')) {
  document.getElementById('urSimpan').addEventListener('click', async () => {
    if (urUserId === null) return;
    const ids = $$('#urDaftar input:checked').map((c) => parseInt(c.value, 10));
    try {
      await api(`/api/users/${urUserId}/routers`, { router_ids: ids });
      toast('Batasan router disimpan', 'ok');
      tutupModal('#modalUserRouter');
    } catch (e) { toast('Gagal: ' + e.message, 'err'); }
  });

  document.getElementById('btnUserSimpan').addEventListener('click', async () => {
    try {
      await api('/api/users', {
        username: document.getElementById('uNama').value.trim(),
        password: document.getElementById('uPass').value,
        role: document.getElementById('uRole').value,
      });
      toast('Pengguna ditambahkan', 'ok');
      document.getElementById('uNama').value = '';
      document.getElementById('uPass').value = '';
      muatUser();
    } catch (e) { toast('Gagal: ' + e.message, 'err'); }
  });
  muatUser();
}

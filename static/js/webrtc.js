/*
  Live audio/video for the lesson room. Two ways of carrying the media; the
  SERVER decides which (ROOM_CONFIG.livekitUrl is set only when a LiveKit media
  server is configured), and everybody in a room always uses the same one.

  SFU (LiveKit)   Every browser sends ONE stream to the media server, which
                  forwards the right quality to each viewer. This is what lets a
                  class of 30 keep cameras on: nobody's laptop or phone has to
                  upload its video 29 times. Used whenever LiveKit is configured.

  Mesh (fallback) Browsers connect directly to each other (peer-to-peer). Fine
                  for small groups. Made sturdier: TURN credentials come from the
                  server, a connection that drops is restarted instead of being
                  thrown away, and when a lesson gets busy new students join with
                  video off (see "mesh_full" in sockets.py).

  Either way, signalling, the lobby, chat, hands, slides, whiteboard and
  recording control stay on Socket.IO, and the rest of the room (tiles,
  participant list, recorder, attendance) talks to the globals set here:
  window._remoteStreams, window.peerMeta, window.getLocalStream() and so on.
  A person's media identity is their Socket.IO sid in both modes.
*/

// Set by the server — see meeting_room.html and rtc.py.
const ICE_SERVERS = {
  iceServers: (window.ROOM_CONFIG && window.ROOM_CONFIG.iceServers) || [],
};

const socket = io();
window._socket = socket;

let localStream = null;
const peerConnections = {}; // sid -> RTCPeerConnection (mesh only)
const peerMeta = {}; // sid -> { name, isHost, sharing, dataMode }
window._remoteStreams = {}; // sid -> { stream, name, isHost } — read by recorder.js
window.peerConnections = peerConnections;
window.peerMeta = peerMeta;
window.__dataMode = "normal"; // "normal" | "low" | "audio" — see lowdata.js

const { roomCode, isHost, userName, hostName, isObserver, livekitUrl } = window.ROOM_CONFIG;
const SFU = !!livekitUrl;
window.MEDIA_MODE = SFU ? "sfu" : "mesh";

// Connections from an observing admin (mesh only). Kept apart from
// peerConnections / peerMeta so they never appear in the participant list or
// filmstrip: the observer receives our media but is not shown to anyone.
const observerConnections = {}; // sid -> RTCPeerConnection
window.observerConnections = observerConnections;

let viewMode = "speaker"; // "speaker" | "gallery"

function notifyParticipantsChanged() {
  window.dispatchEvent(new CustomEvent("participants-changed"));
}

function banner(text, opts) {
  window.dispatchEvent(new CustomEvent("net-banner", { detail: Object.assign({ text }, opts || {}) }));
}

/* ---------------- Camera and microphone ---------------- */

function cameraConstraints() {
  // The media server makes the smaller versions itself, so its teacher can send
  // HD. In the peer-to-peer mesh every extra pixel is sent once PER viewer.
  if (SFU && isHost) return { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
  if (isHost) return true;
  return { width: { ideal: 640 }, height: { ideal: 360 }, frameRate: { ideal: 24 } };
}
window._cameraConstraints = cameraConstraints;

const AUDIO_CONSTRAINTS = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };

async function getLocalMedia() {
  try {
    return await navigator.mediaDevices.getUserMedia({ video: cameraConstraints(), audio: AUDIO_CONSTRAINTS });
  } catch (err) {
    console.warn("Camera+mic unavailable:", err);
  }
  // A broken or refused camera must not also cost the person their voice.
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: AUDIO_CONSTRAINTS });
  } catch (err) {
    console.warn("Mic unavailable, joining without audio/video:", err);
  }
  return new MediaStream();
}

async function initMedia() {
  if (isObserver) {
    // Receive-only: no camera or mic is requested, nothing is sent.
    localStream = new MediaStream();
    window._localStreamRef = localStream;
    socket.emit("request_to_join", { room_code: roomCode, observe: true });
    return;
  }
  localStream = await getLocalMedia();

  window._localStreamRef = localStream;
  window._originalVideoTrack = localStream.getVideoTracks()[0] || null;
  // What we send when NOT screen sharing — the plain camera track.
  window._cameraOrBackgroundTrack = window._originalVideoTrack;

  // Show my own video immediately: spotlight if I'm the host, filmstrip otherwise.
  if (isHost) {
    renderSpotlightStream(localStream, `${userName}`, true);
  } else {
    upsertFilmstripTile("local", localStream, `${userName} (you)`, false);
  }

  socket.emit("request_to_join", { room_code: roomCode });
}

socket.on("join_approved", () => {
  window.dispatchEvent(new CustomEvent("admitted"));
  socket.emit("join", { room_code: roomCode });
});

socket.on("waiting_for_host", () => {
  window.dispatchEvent(new CustomEvent("waiting-for-host"));
});

socket.on("join_denied", () => {
  window.dispatchEvent(new CustomEvent("join-denied"));
});

socket.on("join_request", (data) => {
  window.dispatchEvent(new CustomEvent("join-request", { detail: data }));
});

socket.on("join_request_cleared", (data) => {
  window.dispatchEvent(new CustomEvent("join-request-cleared", { detail: data }));
});

/* ======================================================================
   People list (both modes)
   ====================================================================== */

socket.on("existing_peers", async ({ peers, recording }) => {
  window.dispatchEvent(new CustomEvent("recording-status", { detail: { recording: !!recording } }));
  for (const peer of peers) {
    const meta = { name: peer.name, isHost: peer.is_host, sharing: !!peer.sharing, dataMode: peer.data_mode || "normal" };
    if (SFU) {
      peerMeta[peer.sid] = meta;
    } else {
      await callPeer(peer.sid, meta);
    }
  }
  if (SFU) {
    notifyParticipantsChanged();
    requestSfuToken();
  }
  // Joined (or reloaded) while someone is already presenting: show it straight away.
  for (const peer of peers) {
    if (peer.sharing) {
      window.dispatchEvent(new CustomEvent("screen-share-changed", { detail: { sid: peer.sid, sharing: true } }));
    }
    if (peer.data_mode && peer.data_mode !== "normal") window.setTileDataMode(peer.sid, peer.data_mode);
  }
});

socket.on("peer_joined", async ({ sid, name, is_host }) => {
  // In the mesh the newcomer will call us (see existing_peers on their side),
  // so we just wait. In SFU mode their tracks simply appear.
  peerMeta[sid] = Object.assign(peerMeta[sid] || {}, { name, isHost: is_host });
  notifyParticipantsChanged();
  // Newcomers never call an observer (they aren't told it exists), so the
  // observer calls them instead.
  if (!SFU && isObserver) await callPeer(sid, { name, isHost: is_host });
});

socket.on("peer_left", ({ sid }) => teardownPeer(sid));

socket.on("data_mode", ({ sid, mode }) => {
  if (peerMeta[sid]) peerMeta[sid].dataMode = mode;
  window.setTileDataMode(sid, mode);
  if (!SFU) meshApplyPolicyFor(sid);
  notifyParticipantsChanged();
});

// The lesson is busy and the peer-to-peer mesh can't carry another camera:
// this person joins with video off. (Never sent when a media server is used.)
socket.on("mesh_full", () => {
  // Stop sending MY picture, but keep receiving the teacher's.
  window.__noVideoOut = true;
  meshApplyPolicyAll();
  window.dispatchEvent(new CustomEvent("mesh-full"));
});

function teardownPeer(sid) {
  const pc = peerConnections[sid];
  if (pc) {
    clearTimeout(pc._failTimer);
    pc.close();
    delete peerConnections[sid];
  }
  const meta = peerMeta[sid];
  delete peerMeta[sid];
  delete window._remoteStreams[sid];

  if (meta && meta.isHost) {
    clearSpotlight();
  } else {
    removeFilmstripTile(sid);
  }
  notifyParticipantsChanged();
  if (viewMode === "gallery") renderGalleryView();
}

/* ======================================================================
   MESH (peer-to-peer)
   ====================================================================== */

const LEVEL = { normal: 0, low: 1, audio: 2 };
function worseMode(a, b) {
  return LEVEL[a] >= LEVEL[b] ? a : b;
}

// What this connection should be sending, given MY data mode and THEIRS.
// (If they are on a weak link, we send them less, which is what protects it.)
async function meshApplyPolicy(pc, sid, isObserverConn) {
  const remote = (!isObserverConn && peerMeta[sid] && peerMeta[sid].dataMode) || "normal";
  const mode = worseMode(window.__dataMode || "normal", remote);
  const s = pc.getSenders().find((x) => x._isVideo);
  if (!s) return;
  const wanted = window._outTrack || (localStream && localStream.getVideoTracks()[0]) || null;
  try {
    // Audio-only (chosen, or a student joining a busy lesson): send no picture.
    // The teacher's picture is still RECEIVED by a student in the busy case.
    if (mode === "audio" || (!isHost && window.__noVideoOut)) {
      if (s.track) await s.replaceTrack(null);
      return;
    }
    if (!s.track && wanted) await s.replaceTrack(wanted);
    if (s.getParameters) {
      const params = s.getParameters();
      if (!params.encodings || !params.encodings.length) params.encodings = [{}];
      // In the peer-to-peer mesh the teacher uploads the picture once PER
      // student, all through one phone-or-laptop connection. Left uncapped the
      // browser starves the picture first and keeps the sound, so students
      // "hear but can't see". The more students, the smaller each copy.
      const n = Object.keys(peerConnections).length;
      const hostCap = isHost && !SFU ? (n <= 2 ? 800000 : n <= 4 ? 500000 : n <= 6 ? 350000 : 250000) : 0;
      if (mode === "low") {
        params.encodings[0].maxBitrate = 150000;
        params.encodings[0].scaleResolutionDownBy = 2;
      } else if (hostCap) {
        params.encodings[0].maxBitrate = hostCap;
        if (n > 4) params.encodings[0].scaleResolutionDownBy = 1.5;
        else delete params.encodings[0].scaleResolutionDownBy;
      } else {
        delete params.encodings[0].maxBitrate;
        delete params.encodings[0].scaleResolutionDownBy;
      }
      await s.setParameters(params);
    }
  } catch (err) {
    console.warn("Could not adjust video for a connection:", err);
  }
}

function meshApplyPolicyFor(sid) {
  const pc = peerConnections[sid];
  if (pc) meshApplyPolicy(pc, sid, false);
}

function meshApplyPolicyAll() {
  Object.entries(peerConnections).forEach(([sid, pc]) => meshApplyPolicy(pc, sid, false));
  Object.entries(observerConnections).forEach(([sid, pc]) => meshApplyPolicy(pc, sid, true));
}

function flushCandidates(pc) {
  const pending = pc._pending || [];
  pc._pending = [];
  pending.forEach((c) => pc.addIceCandidate(new RTCIceCandidate(c)).catch((e) => console.warn("ICE candidate", e)));
}

function sendSignal(target, signal) {
  socket.emit("signal", { target, signal });
}

function createPeerConnection(targetSid, meta, isCaller) {
  const pc = new RTCPeerConnection(ICE_SERVERS);
  pc._isCaller = !!isCaller;
  pc._pending = [];
  pc._restarts = 0;
  peerConnections[targetSid] = pc;
  peerMeta[targetSid] = Object.assign(peerMeta[targetSid] || {}, meta);
  meta = peerMeta[targetSid];

  localStream.getTracks().forEach((track) => {
    const s = pc.addTrack(track, localStream);
    if (track.kind === "video") s._isVideo = true;
  });

  pc.onicecandidate = (event) => {
    if (event.candidate) sendSignal(targetSid, { type: "candidate", candidate: event.candidate });
  };

  pc.ontrack = (event) => {
    const known = window._remoteStreams[targetSid];
    const stream = event.streams[0] || (known && known.stream) || new MediaStream();
    if (!stream.getTracks().includes(event.track)) stream.addTrack(event.track);
    watchVideoTrack(event.track, stream);
    window._remoteStreams[targetSid] = { stream, name: meta.name, isHost: meta.isHost };
    if (meta.isHost) {
      renderSpotlightStream(stream, meta.name, false);
    } else {
      upsertFilmstripTile(targetSid, stream, meta.name, true);
    }
    if (viewMode === "gallery") renderGalleryView();
    if (meta.sharing) window.setTileScreenSharing(targetSid, true);
    if (meta.dataMode && meta.dataMode !== "normal") window.setTileDataMode(targetSid, meta.dataMode);
    window.dispatchEvent(new CustomEvent("remote-stream", { detail: { sid: targetSid } }));
  };

  pc.onconnectionstatechange = () => meshConnectionChanged(targetSid, pc);

  if (isHost) meshApplyPolicyAll(); else meshApplyPolicy(pc, targetSid, false);
  notifyParticipantsChanged();
  return pc;
}

// A connection that goes quiet is usually a hiccup (Wi-Fi to mobile data, a
// train tunnel). "disconnected" often heals by itself; "failed" gets an ICE
// restart on the same connection, and only if that keeps failing is a brand
// new connection built. Previously any of these just deleted the person.
function meshConnectionChanged(sid, pc) {
  if (peerConnections[sid] !== pc) return;
  clearTimeout(pc._failTimer);
  const state = pc.connectionState;
  if (state === "connected") {
    pc._restarts = 0;
    return;
  }
  if (state === "closed") return;
  if (state === "disconnected") {
    pc._failTimer = setTimeout(() => meshRecover(sid, pc), 4000);
  } else if (state === "failed") {
    meshRecover(sid, pc);
  }
}

async function meshRecover(sid, pc) {
  if (peerConnections[sid] !== pc || pc.connectionState === "connected" || pc.connectionState === "closed") return;
  pc._restarts += 1;
  if (pc._restarts > 3) return meshRebuild(sid);
  if (pc._isCaller) {
    try {
      const offer = await pc.createOffer({ iceRestart: true });
      await pc.setLocalDescription(offer);
      sendSignal(sid, { type: "offer", sdp: offer });
    } catch (err) {
      console.warn("ICE restart failed:", err);
    }
  } else {
    sendSignal(sid, { type: "restart-request" }); // the other side makes the offer
  }
  pc._failTimer = setTimeout(() => meshRecover(sid, pc), 12000);
}

// Last resort: throw the connection away and make a new one (the tile stays).
async function meshRebuild(sid) {
  const old = peerConnections[sid];
  const meta = peerMeta[sid];
  if (!meta) return;
  meta._rebuilds = (meta._rebuilds || 0) + 1;
  if (old) {
    clearTimeout(old._failTimer);
    old.close();
    delete peerConnections[sid];
  }
  if (meta._rebuilds > 3) {
    banner("A connection to " + (meta.name || "someone") + " could not be restored.", { kind: "warn" });
    return;
  }
  if (old && old._isCaller) await callPeer(sid, meta);
  else sendSignal(sid, { type: "rebuild-request" });
}

async function callPeer(targetSid, meta) {
  const pc = createPeerConnection(targetSid, meta, true);
  // An observer sends nothing, so it must explicitly ask to receive.
  const offer = await pc.createOffer(isObserver ? { offerToReceiveAudio: true, offerToReceiveVideo: true } : undefined);
  await pc.setLocalDescription(offer);
  sendSignal(targetSid, { type: "offer", sdp: offer });
}

function closeObserverConnection(sid) {
  const pc = observerConnections[sid];
  if (pc) {
    pc.close();
    delete observerConnections[sid];
  }
}
socket.on("observer_left", ({ sid }) => closeObserverConnection(sid));

// An observing admin is connecting to us. Answer with our media, but keep it
// completely out of the UI: no tile, no participant, no notification.
async function handleObserverSignal(sender, signal) {
  let pc = observerConnections[sender];
  if (signal.type === "offer") {
    if (!pc) {
      pc = new RTCPeerConnection(ICE_SERVERS);
      pc._pending = [];
      observerConnections[sender] = pc;
      localStream.getTracks().forEach((track) => {
        const s = pc.addTrack(track, localStream);
        if (track.kind === "video") s._isVideo = true;
      });
      pc.onicecandidate = (event) => {
        if (event.candidate) sendSignal(sender, { type: "candidate", candidate: event.candidate });
      };
      pc.onconnectionstatechange = () => {
        if (["failed", "closed"].includes(pc.connectionState)) closeObserverConnection(sender);
      };
      meshApplyPolicy(pc, sender, true);
    }
    await pc.setRemoteDescription(new RTCSessionDescription(signal.sdp));
    flushCandidates(pc);
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    sendSignal(sender, { type: "answer", sdp: answer });
  } else if (signal.type === "candidate" && pc) {
    if (pc.remoteDescription) {
      try {
        await pc.addIceCandidate(new RTCIceCandidate(signal.candidate));
      } catch (err) {
        console.warn("Failed to add ICE candidate", err);
      }
    } else {
      pc._pending.push(signal.candidate);
    }
  }
}

socket.on("signal", async ({ sender, signal, observer }) => {
  if (SFU) return; // no peer-to-peer signalling when a media server carries the call
  if (observer && !isObserver) return handleObserverSignal(sender, signal);
  let pc = peerConnections[sender];

  if (signal.type === "offer") {
    const meta = peerMeta[sender] || { name: "Participant", isHost: false };
    pc = pc || createPeerConnection(sender, meta, false);
    await pc.setRemoteDescription(new RTCSessionDescription(signal.sdp));
    flushCandidates(pc);
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    sendSignal(sender, { type: "answer", sdp: answer });
  } else if (signal.type === "answer") {
    if (pc) {
      await pc.setRemoteDescription(new RTCSessionDescription(signal.sdp));
      flushCandidates(pc);
    }
  } else if (signal.type === "candidate") {
    if (!pc) return;
    // A candidate can arrive while the offer is still being applied.
    if (pc.remoteDescription) {
      try {
        await pc.addIceCandidate(new RTCIceCandidate(signal.candidate));
      } catch (err) {
        console.warn("Failed to add ICE candidate", err);
      }
    } else {
      pc._pending.push(signal.candidate);
    }
  } else if (signal.type === "restart-request") {
    if (pc && pc._isCaller) meshRecover(sender, pc);
  } else if (signal.type === "rebuild-request") {
    const meta = peerMeta[sender];
    if (meta) {
      if (pc) {
        clearTimeout(pc._failTimer);
        pc.close();
        delete peerConnections[sender];
      }
      await callPeer(sender, meta);
    }
  }
});

/* ======================================================================
   SFU (LiveKit)
   ====================================================================== */

let lkRoom = null;
let lkRetries = 0;
let lkAskedForToken = false;
const lkSpeakerOrder = []; // most recent speaker first (identities)
let lkApplyTimer = null;

function requestSfuToken() {
  if (lkAskedForToken && lkRoom) return;
  lkAskedForToken = true;
  socket.emit("livekit_token", {});
}

socket.on("livekit_token", ({ url, token }) => {
  connectSfu(url, token).catch((err) => {
    console.error("Could not join the media server:", err);
    scheduleSfuRetry();
  });
});

function scheduleSfuRetry() {
  if (window.__sessionOver) return;
  lkRetries += 1;
  if (lkRetries > 6) {
    banner("Can't reach the video server. Check your internet connection, then reload this page.", { kind: "error", sticky: true });
    return;
  }
  banner("Connection lost. Reconnecting…", { kind: "warn" });
  setTimeout(() => socket.emit("livekit_token", {}), Math.min(15000, 1000 * 2 ** lkRetries));
}

function sfuMeta(participant) {
  const sid = participant.identity;
  let host = false;
  try { host = !!JSON.parse(participant.metadata || "{}").host; } catch (e) { /* ignore */ }
  if (!peerMeta[sid]) {
    peerMeta[sid] = { name: participant.name || "Participant", isHost: host };
    notifyParticipantsChanged();
  } else if (host && !peerMeta[sid].isHost) {
    // The teacher's details can arrive a moment after the teacher does.
    peerMeta[sid].isHost = true;
    notifyParticipantsChanged();
  }
  return peerMeta[sid];
}

async function publishLocal() {
  const LK = window.LivekitClient;
  const lp = lkRoom.localParticipant;
  for (const track of localStream.getTracks()) {
    if (track.readyState !== "live") continue;
    const isVideo = track.kind === "video";
    await lp.publishTrack(track, {
      source: isVideo ? LK.Track.Source.Camera : LK.Track.Source.Microphone,
      name: isVideo ? "camera" : "mic",
      simulcast: isVideo,
      dtx: !isVideo,
      red: !isVideo,
    });
  }
  sfuApplyLocalPolicy();
}

async function connectSfu(url, token) {
  const LK = window.LivekitClient;
  if (!LK) {
    banner("The video library could not be loaded. Check your internet connection and reload.", { kind: "error", sticky: true });
    return;
  }
  if (lkRoom) {
    const old = lkRoom;
    lkRoom = null;
    try { old.removeAllListeners(); old.disconnect(); } catch (e) { /* ignore */ }
  }
  const room = new LK.Room({
    adaptiveStream: false, // we choose each person's quality ourselves (sfuApplySubscriptions)
    dynacast: true, // nobody pays to upload a quality nobody is watching
    publishDefaults: {
      simulcast: true,
      videoSimulcastLayers: [LK.VideoPresets.h180, LK.VideoPresets.h360],
    },
  });
  lkRoom = room;

  room.on(LK.RoomEvent.TrackSubscribed, sfuTrackSubscribed);
  room.on(LK.RoomEvent.TrackUnsubscribed, sfuTrackUnsubscribed);
  room.on(LK.RoomEvent.TrackPublished, () => sfuQueueApply());
  room.on(LK.RoomEvent.ParticipantConnected, (p) => { sfuMeta(p); sfuQueueApply(); });
  room.on(LK.RoomEvent.ParticipantMetadataChanged, (md, p) => { if (p) { sfuMeta(p); sfuQueueApply(); } });
  room.on(LK.RoomEvent.ParticipantDisconnected, (p) => teardownPeer(p.identity));
  room.on(LK.RoomEvent.ActiveSpeakersChanged, (speakers) => {
    speakers.forEach((s) => {
      const i = lkSpeakerOrder.indexOf(s.identity);
      if (i >= 0) lkSpeakerOrder.splice(i, 1);
      lkSpeakerOrder.unshift(s.identity);
    });
    lkSpeakerOrder.length = Math.min(lkSpeakerOrder.length, 12);
    sfuQueueApply();
  });
  room.on(LK.RoomEvent.ConnectionQualityChanged, (quality, participant) => {
    if (participant && participant.isLocal) {
      window.dispatchEvent(new CustomEvent("link-quality", { detail: { quality } }));
    }
  });
  room.on(LK.RoomEvent.Reconnecting, () => banner("Connection weak. Reconnecting…", { kind: "warn" }));
  room.on(LK.RoomEvent.Reconnected, () => { lkRetries = 0; banner("", { clear: true }); sfuQueueApply(); });
  room.on(LK.RoomEvent.Disconnected, () => {
    if (lkRoom !== room || window.__sessionOver) return;
    scheduleSfuRetry();
  });

  await room.connect(url, token, { autoSubscribe: false });
  lkRetries = 0;
  banner("", { clear: true });
  if (!isObserver) await publishLocal();
  room.remoteParticipants.forEach((p) => sfuMeta(p));
  sfuApplySubscriptions();
}

function sfuTrackSubscribed(track, pub, participant) {
  const sid = participant.identity;
  const meta = sfuMeta(participant);
  let entry = window._remoteStreams[sid];
  if (!entry) {
    entry = { stream: new MediaStream(), name: meta.name, isHost: meta.isHost };
    window._remoteStreams[sid] = entry;
  }
  entry.stream.addTrack(track.mediaStreamTrack);
  watchVideoTrack(track.mediaStreamTrack, entry.stream);

  if (meta.isHost) {
    const v = document.querySelector("#spotlight-tile video");
    if (!v || v.srcObject !== entry.stream) renderSpotlightStream(entry.stream, meta.name, false);
  } else if (!document.getElementById(`tile-${sid}`)) {
    upsertFilmstripTile(sid, entry.stream, meta.name, true);
  }
  if (viewMode === "gallery") renderGalleryView();
  // The sound usually arrives before the picture. A video track added to a
  // stream that is already on screen is not always painted by phones, so
  // re-attach the stream now that the picture is part of it.
  if (track.kind === "video") refreshStreamVideos(entry.stream);
  if (meta.sharing) window.setTileScreenSharing(sid, true);
  if (meta.dataMode && meta.dataMode !== "normal") window.setTileDataMode(sid, meta.dataMode);
  window.dispatchEvent(new CustomEvent("remote-stream", { detail: { sid } }));
}

function sfuTrackUnsubscribed(track, pub, participant) {
  const entry = window._remoteStreams[participant.identity];
  if (entry && track.mediaStreamTrack) entry.stream.removeTrack(track.mediaStreamTrack);
}

function sfuQueueApply() {
  clearTimeout(lkApplyTimer);
  lkApplyTimer = setTimeout(sfuApplySubscriptions, 300);
}

// Decide whose video this browser receives, and at what quality. Audio is
// always received (it is cheap). A student's phone doesn't need 29 other
// cameras: it gets the teacher, anyone presenting, and the few people talking.
function sfuApplySubscriptions() {
  if (!lkRoom) return;
  const LK = window.LivekitClient;
  const mode = window.__dataMode || "normal";
  const maxOthers = mode === "low" ? 2 : (isHost || isObserver ? 24 : 8);

  const people = Array.from(lkRoom.remoteParticipants.values());
  const rank = (p) => {
    const m = peerMeta[p.identity] || {};
    if (m.isHost) return 0;
    if (m.sharing || p.identity === window.__spotlightSid) return 1;
    const i = lkSpeakerOrder.indexOf(p.identity);
    return i >= 0 ? 2 + i / 100 : 3;
  };
  people.sort((a, b) => rank(a) - rank(b));

  let others = 0;
  people.forEach((p) => {
    const m = peerMeta[p.identity] || {};
    const important = !!(m.isHost || m.sharing || p.identity === window.__spotlightSid);
    p.trackPublications.forEach((pub) => {
      if (pub.kind === "audio") {
        if (!pub.isSubscribed) pub.setSubscribed(true);
        return;
      }
      let want;
      if (mode === "audio") want = false;
      else if (important) want = true;
      else { want = others < maxOthers; if (want) others += 1; }
      if (pub.isSubscribed !== want) pub.setSubscribed(want);
      if (want) {
        const q = important ? (mode === "low" ? LK.VideoQuality.MEDIUM : LK.VideoQuality.HIGH) : LK.VideoQuality.LOW;
        try { pub.setVideoQuality(q); } catch (e) { /* not subscribed yet: applied on the next pass */ }
      }
    });
  });
}

// What I send: smaller in low-data mode, nothing at all in audio-only.
async function sfuApplyLocalPolicy() {
  if (!lkRoom || isObserver) return;
  const LK = window.LivekitClient;
  const lp = lkRoom.localParticipant;
  const mode = window.__dataMode || "normal";
  const pub = lp.getTrackPublication(LK.Track.Source.Camera);
  const cam = window._originalVideoTrack;
  try {
    if (mode === "audio") {
      if (pub && pub.track) await lp.unpublishTrack(pub.track, false); // keeps the camera itself running
    } else {
      if (!pub && (window._outTrack || cam) && (window._outTrack || cam).readyState === "live") {
        await lp.publishTrack(window._outTrack || cam, { source: LK.Track.Source.Camera, name: "camera", simulcast: true });
      }
      const p2 = lp.getTrackPublication(LK.Track.Source.Camera);
      if (p2 && p2.track && p2.track.setPublishingQuality) {
        p2.track.setPublishingQuality(mode === "low" ? LK.VideoQuality.LOW : LK.VideoQuality.HIGH);
      }
    }
  } catch (err) {
    console.warn("Could not adjust the video I send:", err);
  }
}

/* ======================================================================
   Data mode (both modes) — called by lowdata.js
   ====================================================================== */

async function constrainCamera(mode) {
  const cam = window._originalVideoTrack;
  if (!cam || cam.readyState !== "live" || window._outTrack && window._outTrack !== cam) return; // sharing: leave it
  try {
    if (mode === "normal") {
      const c = cameraConstraints();
      await cam.applyConstraints(c === true ? { width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 30 } } : c);
    } else {
      await cam.applyConstraints({ width: { ideal: 320 }, height: { ideal: 180 }, frameRate: { ideal: 15 } });
    }
  } catch (err) {
    console.warn("Camera could not be resized:", err);
  }
}

window.setDataMode = function (mode) {
  if (!LEVEL.hasOwnProperty(mode)) mode = "normal";
  window.__dataMode = mode;
  if (!isObserver) socket.emit("data_mode", { mode });
  constrainCamera(mode);
  if (SFU) {
    sfuApplySubscriptions();
    sfuApplyLocalPolicy();
  } else {
    meshApplyPolicyAll();
  }
  window.dispatchEvent(new CustomEvent("data-mode-changed", { detail: { mode } }));
};

/* ---------------- Screen share support ---------------- */

// Swaps the outgoing video track on every open connection (used by screen
// sharing — anything that needs to replace what we're sending without a full
// renegotiation).
window.replaceOutgoingVideoTrack = function (newTrack) {
  window._outTrack = newTrack;
  if (SFU) {
    if (!lkRoom || isObserver || !newTrack) return;
    const LK = window.LivekitClient;
    const lp = lkRoom.localParticipant;
    const pub = lp.getTrackPublication(LK.Track.Source.Camera);
    if (pub && pub.track) {
      pub.track.replaceTrack(newTrack).catch((e) => console.warn("replaceTrack failed", e));
    } else if ((window.__dataMode || "normal") !== "audio") {
      lp.publishTrack(newTrack, { source: LK.Track.Source.Camera, name: "camera", simulcast: true }).catch((e) => console.warn(e));
    }
    return;
  }
  Object.values(peerConnections).concat(Object.values(observerConnections)).forEach((pc) => {
    const sender = pc.getSenders().find((s) => s._isVideo || (s.track && s.track.kind === "video"));
    if (sender && sender.track) sender.replaceTrack(newTrack);
  });
  meshApplyPolicyAll();
};

// Swaps the video track inside localStream itself, so every <video> element
// bound via srcObject=localStream (spotlight/filmstrip self-preview) updates
// automatically without re-binding.
window.setLocalVideoTrack = function (newTrack) {
  const oldTrack = localStream.getVideoTracks()[0];
  if (oldTrack) localStream.removeTrack(oldTrack);
  if (newTrack) localStream.addTrack(newTrack);
  if (newTrack === window._originalVideoTrack) window._outTrack = null;
};

window.getLocalStream = function () {
  return localStream;
};

// People presenting are always worth full quality, so re-decide who is watched.
window.addEventListener("screen-share-changed", () => { if (SFU) sfuQueueApply(); });
// ...and so is a student the teacher has put in the spotlight (see classview.js).
window.addEventListener("spotlight-changed", () => { if (SFU) sfuQueueApply(); });

/* ---------------- DOM rendering ---------------- */

// Phone browsers (iPhone Safari especially) are fussy about a <video> that is
// fed by a live MediaStream: a picture that arrives AFTER the sound, or a
// <video> that was never told to play, can stay black while the audio plays.
// These helpers make sure the picture is (re)attached and playing.
function startPlayback(video) {
  video.setAttribute("playsinline", "");
  video.setAttribute("webkit-playsinline", "");
  const p = video.play();
  if (p && p.catch) {
    p.catch(() => {
      // Sound-on autoplay was refused: show the picture muted rather than nothing,
      // and give the sound back at the first touch.
      if (!video.muted) {
        video.muted = true;
        video._wantsSound = true;
        video.play().catch(() => {});
      }
    });
  }
}

function giveSoundBack() {
  document.querySelectorAll("video").forEach((v) => {
    if (v._wantsSound) {
      v.muted = false;
      v._wantsSound = false;
      v.play().catch(() => {});
    }
  });
}
document.addEventListener("touchend", giveSoundBack, { passive: true });
document.addEventListener("click", giveSoundBack);

// Re-attach a stream to every <video> showing it (used when a video track is
// added to a stream that is already on screen, e.g. the sound came first).
function refreshStreamVideos(stream) {
  document.querySelectorAll("video").forEach((v) => {
    if (v.srcObject !== stream) return;
    v.srcObject = null;
    v.srcObject = stream;
    startPlayback(v);
  });
}

// A new video track starts "muted" (no frames yet). Phones sometimes do not
// paint it once frames start, so re-attach at that moment.
function watchVideoTrack(track, stream) {
  if (!track || track.kind !== "video" || track._watched) return;
  track._watched = true;
  track.addEventListener("unmute", () => refreshStreamVideos(stream));
}

function renderSpotlightStream(stream, name, isSelf) {
  const tile = document.getElementById("spotlight-tile");
  const existingVideo = tile.querySelector("video");
  const avatar = document.getElementById("spotlight-avatar");
  if (avatar) avatar.style.display = "none";

  const label = document.getElementById("spotlight-label");
  label.innerHTML = `<span class="host-badge">Host</span> ${name}${isSelf ? " (you)" : ""}`;

  // Same stream already showing: keep the element (re-creating it for every
  // track is what makes phones lose the picture).
  if (existingVideo && existingVideo.srcObject === stream) return;
  if (existingVideo) existingVideo.remove();

  const video = document.createElement("video");
  video.autoplay = true;
  video.playsInline = true;
  video.muted = isSelf;
  video.srcObject = stream;
  tile.insertBefore(video, tile.firstChild);
  startPlayback(video);
}

function clearSpotlight() {
  const tile = document.getElementById("spotlight-tile");
  const video = tile.querySelector("video");
  if (video) video.remove();
  const avatar = document.getElementById("spotlight-avatar");
  if (avatar) avatar.style.display = "flex";
  document.getElementById("spotlight-label").innerHTML =
    `<span class="host-badge">Host</span> ${hostName}`;
}

function upsertFilmstripTile(key, stream, name, isRemote) {
  const empty = document.getElementById("filmstrip-empty");
  if (empty) empty.remove();

  let tile = document.getElementById(`tile-${key}`);
  if (!tile) {
    tile = document.createElement("div");
    tile.className = "mini-tile";
    tile.id = `tile-${key}`;
    document.getElementById("filmstrip").appendChild(tile);
  }
  tile.innerHTML = "";
  tile.classList.remove("is-screen");
  tile.dataset.name = name;

  const video = document.createElement("video");
  video.autoplay = true;
  video.playsInline = true;
  video.muted = !isRemote; // mute local self-view to avoid echo
  video.srcObject = stream;
  tile.appendChild(video);
  startPlayback(video);

  const label = document.createElement("div");
  label.className = "tile-label";
  label.textContent = name;
  tile.appendChild(label);

  if (viewMode === "gallery") renderGalleryView();
}

function removeFilmstripTile(key) {
  const tile = document.getElementById(`tile-${key}`);
  if (tile) tile.remove();

  const strip = document.getElementById("filmstrip");
  if (!strip.querySelector(".mini-tile")) {
    const empty = document.createElement("div");
    empty.className = "filmstrip-empty";
    empty.id = "filmstrip-empty";
    empty.textContent = "Waiting for others to join…";
    strip.appendChild(empty);
  }
}

// Sets/clears a small raised-hand badge on a participant's tile label.
window.setTileHandRaised = function (sid, raised) {
  const key = sid === "local" ? "local" : sid;
  const isSpotlightSelf = isHost && sid === "local";
  const isSpotlightHost = peerMeta[sid] && peerMeta[sid].isHost;

  let labelEl;
  if (isSpotlightSelf || isSpotlightHost) {
    labelEl = document.getElementById("spotlight-label");
  } else {
    const tile = document.getElementById(`tile-${key}`);
    labelEl = tile ? tile.querySelector(".tile-label") : null;
  }
  if (!labelEl) return;

  const existingBadge = labelEl.querySelector(".hand-badge");
  if (raised && !existingBadge) {
    const badge = document.createElement("span");
    badge.className = "hand-badge";
    badge.innerHTML = window.iconHTML("hand");
    badge.title = "Hand raised";
    labelEl.appendChild(badge);
  } else if (!raised && existingBadge) {
    existingBadge.remove();
  }
};

// Marks a tile as being on a weak connection: "low data" or "audio only".
window.setTileDataMode = function (sid, mode) {
  const meta = peerMeta[sid];
  const isSpot = sid !== "local" && meta && meta.isHost;
  const tileEl = document.getElementById(`tile-${sid}`);
  const labelEl = isSpot
    ? document.getElementById("spotlight-label")
    : (tileEl ? tileEl.querySelector(".tile-label") : null);
  if (!labelEl) return;
  let badge = labelEl.querySelector(".data-badge");
  if (mode && mode !== "normal") {
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "data-badge";
      labelEl.appendChild(badge);
    }
    badge.innerHTML = window.iconHTML(mode === "audio" ? "headphones" : "lowData") + (mode === "audio" ? " audio only" : " low data");
    badge.title = mode === "audio" ? "On a weak connection: audio only" : "On a weak connection: smaller video";
  } else if (badge) {
    badge.remove();
  }
  const tile = document.getElementById(`tile-${sid}`);
  if (tile) tile.classList.toggle("no-video", mode === "audio");
};

// Sets/clears a "Presenting" badge on a participant's tile. A tile that is
// presenting shows the WHOLE shared screen (object-fit: contain) instead of
// cropping it like a webcam picture.
window.setTileScreenSharing = function (sid, sharing) {
  let tile;
  if (sid === "local") {
    tile = isHost ? document.getElementById("spotlight-tile") : document.getElementById("tile-local");
  } else {
    if (peerMeta[sid]) peerMeta[sid].sharing = !!sharing;
    tile = peerMeta[sid] && peerMeta[sid].isHost
      ? document.getElementById("spotlight-tile")
      : document.getElementById(`tile-${sid}`);
  }
  if (!tile) return;
  tile.classList.toggle("is-screen", !!sharing);
  let badge = tile.querySelector(".presenting-badge");
  if (sharing && !badge) {
    badge = document.createElement("div");
    badge.className = "presenting-badge";
    badge.innerHTML = window.iconHTML("screenShare") + " Presenting";
    tile.appendChild(badge);
  } else if (!sharing && badge) {
    badge.remove();
  }
};

/* ---------------- View mode: speaker vs gallery ---------------- */

window.setViewMode = function (mode) {
  viewMode = mode;
  const stageArea = document.getElementById("stage-area");
  const gallery = document.getElementById("gallery-grid");
  if (mode === "gallery") {
    stageArea.style.display = "none";
    gallery.style.display = "grid";
    renderGalleryView();
  } else {
    gallery.style.display = "none";
    stageArea.style.display = "flex";
    // Move tiles back to their normal homes (moving, not cloning, keeps
    // the <video> elements' live playback state intact).
    document.querySelector(".stage-area").insertBefore(
      document.getElementById("spotlight-tile"),
      document.getElementById("filmstrip-wrap")
    );
    const filmstrip = document.getElementById("filmstrip");
    Array.from(gallery.querySelectorAll(".mini-tile")).forEach((t) => filmstrip.appendChild(t));
    if (!filmstrip.querySelector(".mini-tile")) {
      const empty = document.createElement("div");
      empty.className = "filmstrip-empty";
      empty.id = "filmstrip-empty";
      empty.textContent = "Waiting for others to join…";
      filmstrip.appendChild(empty);
    }
  }
  window.dispatchEvent(new CustomEvent("view-mode-changed", { detail: { mode } }));
};

function renderGalleryView() {
  const gallery = document.getElementById("gallery-grid");
  const spotlight = document.getElementById("spotlight-tile");
  gallery.appendChild(spotlight); // move (not clone) — preserves video playback
  document.querySelectorAll("#filmstrip .mini-tile").forEach((t) => gallery.appendChild(t));
}

window.getViewMode = function () {
  return viewMode;
};

initMedia();

(function () {
      "use strict";
      const B = window.AstrBotPluginPage;
      const $ = (sel) => document.querySelector(sel);
      const state = {
        page: 1,
        pageSize: 24,
        q: "",
        status: "",
        tag: "",
        total: 0,
        items: [],
        inbox: [],
        inboxTotal: 0,
        tags: {},
        settings: {},
      };
      let current = null;
      let toastTimer = 0;
      let searchTimer = 0;
      let deleteArmed = 0;
      let deleteTimer = 0;
      let reclassifyAllArmed = false;
      let reclassifyAllTimer = 0;
      let confirmAllArmed = false;
      let confirmAllTimer = 0;
      let inboxDeleteArmed = "";

      function esc(value) {
        return String(value == null ? "" : value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#39;");
      }

      function toast(message, isError) {
        const el = $("#toast");
        el.textContent = String(message);
        el.classList.toggle("error", !!isError);
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
      }

      function requireBridge() {
        if (!B) throw new Error("没有检测到 AstrBot 插件页面桥接，请从插件详情页打开。");
        return B;
      }

      async function apiGet(path, params) {
        return requireBridge().apiGet(path, params || {});
      }
      async function apiPost(path, body) {
        return requireBridge().apiPost(path, body || {});
      }

      function statusLabel(status) {
        return ({ ready: "可用", pending: "待分类", disabled: "已停用" })[status] || status;
      }

      function usageLabel(usage) {
        return ({ both: "both", reaction: "reaction", augment: "augment" })[usage] || usage;
      }

      function renderStats(stats) {
        const s = (stats && stats.library_status) || (stats && stats.status) || {};
        const total = stats && stats.confirmed != null ? stats.confirmed : (stats.total || 0);
        const cards = [
          ["total", "表情库", total, ""],
          ["ready", "可用", s.ready || 0, "ready"],
          ["pending", "待分类", s.pending || 0, "pending"],
          ["disabled", "已停用", s.disabled || 0, "disabled"],
        ];
        $("#stats").innerHTML = cards.map(([key, label, value, filter]) => (
          '<div class="stat' + (state.status === filter ? " active" : "") + '" data-status="' + filter + '">' +
            "<b>" + value + "</b><span>" + label + "</span>" +
          "</div>"
        )).join("");
        document.querySelectorAll(".stat").forEach((el) => {
          el.addEventListener("click", () => {
            state.status = el.dataset.status || "";
            $("#statusFilter").value = state.status;
            state.page = 1;
            load();
          });
        });
      }

      function renderInbox() {
        const items = state.inbox || [];
        $("#inboxCount").textContent = state.inboxTotal || items.length;
        const list = $("#inboxList");
        $("#confirmAllBtn").disabled = !items.length;
        if (!items.length) {
          list.innerHTML = '<div class="inbox-empty">暂无待确认的新表情包。自动学习、网页上传后会自动出现在这里。</div>';
          return;
        }
        list.innerHTML = items.map((item) => {
          const tags = (item.tag_labels || []).slice(0, 3).map((t) => (
            '<span class="tag">' + esc(t) + "</span>"
          )).join("");
          const thumb = item.thumb
            ? '<img class="inbox-thumb" loading="lazy" src="' + item.thumb + '" alt="">'
            : '<div class="inbox-thumb"></div>';
          return (
            '<div class="inbox-card" data-id="' + esc(item.id) + '">' +
              thumb +
              '<div class="inbox-info">' +
                '<div class="inbox-desc">' + esc(item.description || "（待分类）") + "</div>" +
                '<div class="inbox-tags">' + tags + "</div>" +
                '<div class="inbox-card-actions">' +
                  '<button class="btn primary" data-inbox-action="confirm">确认加入</button>' +
                  '<button class="btn ghost danger" data-inbox-action="delete">删除</button>' +
                "</div>" +
              "</div>" +
            "</div>"
          );
        }).join("");
      }

      async function confirmInbox(item) {
        try {
          await apiPost("confirm", { id: item.id });
          toast("已确认，移入表情库");
          await load();
        } catch (error) {
          toast(error.message || String(error), true);
        }
      }

      async function deleteInbox(item, button) {
        if (inboxDeleteArmed !== item.id) {
          inboxDeleteArmed = item.id;
          button.textContent = "再点一次";
          setTimeout(() => {
            if (inboxDeleteArmed === item.id) {
              inboxDeleteArmed = "";
              button.textContent = "删除";
            }
          }, 3200);
          return;
        }
        inboxDeleteArmed = "";
        button.disabled = true;
        try {
          await apiPost("delete", { id: item.id });
          toast("已删除");
          await load();
        } catch (error) {
          toast(error.message || String(error), true);
          button.disabled = false;
        }
      }

      function renderTagFilter(tags) {
        const select = $("#tagFilter");
        const keep = state.tag;
        select.innerHTML = '<option value="">全部标签</option>' + Object.entries(tags || {})
          .sort((a, b) => String(a[1]).localeCompare(String(b[1]), "zh-CN"))
          .map(([key, label]) => '<option value="' + esc(key) + '">' + esc(label) + "</option>")
          .join("");
        select.value = keep;
      }

      function cardHtml(item) {
        const tags = (item.tag_labels || []).slice(0, 4).map((t) => (
          '<span class="tag">' + esc(t) + "</span>"
        )).join("");
        const thumb = item.thumb
          ? '<img loading="lazy" src="' + item.thumb + '" alt="">'
          : '<div class="hint" style="padding:20px">无预览</div>';
        const safe = item.safe ? "" : '<span class="badge" style="left:auto;right:8px;background:rgba(220,38,38,.9)">不安全</span>';
        return (
          '<article class="card" data-id="' + esc(item.id) + '">' +
            '<div class="card-media">' + thumb +
              '<span class="badge ' + esc(item.status) + '">' + statusLabel(item.status) + "</span>" + safe +
            "</div>" +
            '<div class="card-body">' +
              '<div class="desc">' + esc(item.description || "（无描述）") + "</div>" +
              '<div class="tags">' + tags + "</div>" +
              '<div class="meta"><span>' + usageLabel(item.usage) + "</span><span>强度 " + item.intensity + "/5</span><span>用 " + item.use_count + " 次</span></div>" +
              '<div class="card-actions">' +
                '<button class="btn small" data-action="edit">编辑</button>' +
                '<button class="btn small ghost" data-action="reclassify">重分类</button>' +
                '<button class="btn small ghost" data-action="toggle">' + (item.status === "disabled" ? "启用" : "停用") + "</button>" +
              "</div>" +
            "</div>" +
          "</article>"
        );
      }

      function renderGrid() {
        const grid = $("#grid");
        const empty = $("#empty");
        if (!state.items.length) {
          grid.innerHTML = "";
          empty.hidden = false;
          return;
        }
        empty.hidden = true;
        grid.innerHTML = state.items.map(cardHtml).join("");
      }

      function renderPager() {
        const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
        $("#pageInfo").textContent = "第 " + state.page + " / " + pages + " 页";
        $("#prev").disabled = state.page <= 1;
        $("#next").disabled = state.page >= pages;
      }

      async function load() {
        $("#loading").hidden = false;
        $("#grid").innerHTML = "";
        try {
          const data = await apiGet("list", {
            page: state.page,
            page_size: state.pageSize,
            q: state.q,
            status: state.status,
            tag: state.tag,
          });
          state.items = data.items || [];
          state.total = data.total || 0;
          state.inbox = data.inbox || [];
          state.inboxTotal = data.inbox_total || 0;
          state.tags = data.tags || {};
          state.settings = data.settings || {};
          renderInbox();
          renderStats(data.stats || {});
          renderTagFilter(state.tags);
          renderGrid();
          renderPager();
          renderSafety();
        } catch (error) {
          toast(error.message || String(error), true);
          $("#empty").hidden = false;
        } finally {
          $("#loading").hidden = true;
        }
      }

      function renderSafety() {
        const enabled = state.settings.safety_filter_enabled !== false;
        const btn = $("#safetyBtn");
        btn.textContent = "安全过滤：" + (enabled ? "开" : "关");
        btn.style.color = enabled ? "" : "var(--warn)";
      }

      function openModal(item) {
        current = item;
        deleteArmed = 0;
        $("#deleteBtn").textContent = "删除";
        $("#deleteBtn").disabled = false;
        $("#editImage").src = item.thumb || "";
        $("#editDescription").value = item.description || "";
        renderTagPicker(item.tags || []);
        $("#editKeywords").value = (item.keywords || []).join(",");
        $("#editUsage").value = item.usage || "both";
        $("#editIntensity").value = item.intensity || 3;
        $("#intensityValue").textContent = item.intensity || 3;
        $("#editSafe").checked = !!item.safe;
        $("#editStatus").value = item.status || "ready";
        $("#editMeta").textContent = "ID: " + item.id + " · 使用 " + item.use_count + " 次";
        $("#modal").hidden = false;
        apiGet("preview/" + encodeURIComponent(item.id)).then((data) => {
          if (data && data.data_url) $("#editImage").src = data.data_url;
        }).catch(() => {});
      }

      function renderTagPicker(selected) {
        const chosen = new Set(selected || []);
        const all = Object.entries(state.tags || {});
        $("#tagPicker").innerHTML = all.map(([key, label]) => (
          '<button type="button" class="tag-choice' + (chosen.has(key) ? " active" : "") +
          '" data-tag="' + esc(key) + '">' + esc(label) + "</button>"
        )).join("");
      }

      function selectedTags() {
        return Array.from(document.querySelectorAll("#tagPicker .tag-choice.active"))
          .map((el) => el.dataset.tag)
          .filter(Boolean);
      }

      function closeModal() {
        current = null;
        deleteArmed = 0;
        $("#deleteBtn").textContent = "删除";
        $("#deleteBtn").disabled = false;
        $("#modal").hidden = true;
      }

      function splitWords(value) {
        return String(value || "").split(/[,，、|;；\s]+/).map((s) => s.trim()).filter(Boolean);
      }

      async function saveModal() {
        if (!current) return;
        const body = {
          id: current.id,
          description: $("#editDescription").value,
          tags: selectedTags(),
          keywords: splitWords($("#editKeywords").value),
          usage: $("#editUsage").value,
          intensity: Number($("#editIntensity").value),
          safe: $("#editSafe").checked,
          status: $("#editStatus").value,
        };
        $("#saveBtn").disabled = true;
        try {
          await apiPost("update", body);
          toast("已保存");
          closeModal();
          await load();
        } catch (error) {
          toast(error.message || String(error), true);
        } finally {
          $("#saveBtn").disabled = false;
        }
      }

      async function deleteCurrent() {
        if (!current) return;
        if (deleteArmed !== current.id) {
          deleteArmed = current.id;
          $("#deleteBtn").textContent = "再点一次确认删除";
          clearTimeout(deleteTimer);
          deleteTimer = setTimeout(() => {
            deleteArmed = 0;
            $("#deleteBtn").textContent = "删除";
          }, 3500);
          return;
        }
        clearTimeout(deleteTimer);
        deleteArmed = 0;
        $("#deleteBtn").disabled = true;
        $("#deleteBtn").textContent = "删除中…";
        try {
          await apiPost("delete", { id: current.id });
          toast("已删除");
          closeModal();
          await load();
        } catch (error) {
          toast(error.message || String(error), true);
        } finally {
          $("#deleteBtn").disabled = false;
          $("#deleteBtn").textContent = "删除";
        }
      }

      async function reclassifyCurrent() {
        if (!current) return;
        try {
          await apiPost("reclassify", { id: current.id });
          toast("已加入分类队列，几秒后刷新");
          setTimeout(load, 2500);
        } catch (error) {
          toast(error.message || String(error), true);
        }
      }

      async function retagCurrent() {
        if (!current) return;
        $("#retagBtn").disabled = true;
        try {
          const data = await apiPost("retag", {
            id: current.id,
            description: $("#editDescription").value,
            keywords: splitWords($("#editKeywords").value),
            update_keywords: $("#retagKeywords").checked,
          });
          if (data && data.item) {
            current = data.item;
            renderTagPicker(current.tags || []);
            $("#editKeywords").value = (current.keywords || []).join(",");
            $("#editIntensity").value = current.intensity || 3;
            $("#intensityValue").textContent = current.intensity || 3;
            $("#editUsage").value = current.usage || "both";
          }
          toast("已按描述重算标签");
          await load();
        } catch (error) {
          toast(error.message || String(error), true);
        } finally {
          $("#retagBtn").disabled = false;
        }
      }

      async function toggleItem(item) {
        const next = item.status === "disabled" ? "ready" : "disabled";
        try {
          await apiPost("update", {
            id: item.id,
            status: next,
            safe: next === "ready" ? true : item.safe,
          });
          toast(next === "ready" ? "已启用" : "已停用");
          await load();
        } catch (error) {
          toast(error.message || String(error), true);
        }
      }

      function bind() {
        $("#search").addEventListener("input", (event) => {
          clearTimeout(searchTimer);
          searchTimer = setTimeout(() => {
            state.q = event.target.value.trim();
            state.page = 1;
            load();
          }, 300);
        });
        $("#statusFilter").addEventListener("change", (event) => {
          state.status = event.target.value;
          state.page = 1;
          load();
        });
        $("#tagFilter").addEventListener("change", (event) => {
          state.tag = event.target.value;
          state.page = 1;
          load();
        });
        $("#refreshBtn").addEventListener("click", load);
        $("#prev").addEventListener("click", () => {
          if (state.page > 1) { state.page -= 1; load(); }
        });
        $("#next").addEventListener("click", () => {
          if (state.page * state.pageSize < state.total) { state.page += 1; load(); }
        });
        $("#confirmAllBtn").addEventListener("click", async () => {
          const btn = $("#confirmAllBtn");
          if (!state.inboxTotal) return;
          if (!confirmAllArmed) {
            confirmAllArmed = true;
            btn.textContent = "再点一次确认全部";
            clearTimeout(confirmAllTimer);
            confirmAllTimer = setTimeout(() => {
              confirmAllArmed = false;
              btn.textContent = "全部确认";
            }, 3500);
            return;
          }
          clearTimeout(confirmAllTimer);
          confirmAllArmed = false;
          btn.textContent = "全部确认";
          btn.disabled = true;
          try {
            const data = await apiPost("confirm", { all: true });
            toast("已确认 " + (data.confirmed || 0) + " 张");
            await load();
          } catch (error) {
            toast(error.message || String(error), true);
          } finally {
            btn.disabled = false;
          }
        });
        $("#inboxList").addEventListener("click", (event) => {
          const action = event.target.closest("button[data-inbox-action]");
          const card = event.target.closest(".inbox-card");
          if (!card) return;
          const item = state.inbox.find((x) => x.id === card.dataset.id);
          if (!item) return;
          if (!action) {
            openModal(item);
            return;
          }
          if (action.dataset.inboxAction === "confirm") confirmInbox(item);
          if (action.dataset.inboxAction === "delete") deleteInbox(item, action);
        });
        $("#reclassifyAll").addEventListener("click", async () => {
          const btn = $("#reclassifyAll");
          if (!reclassifyAllArmed) {
            reclassifyAllArmed = true;
            btn.textContent = "再点一次确认全部分类";
            clearTimeout(reclassifyAllTimer);
            reclassifyAllTimer = setTimeout(() => {
              reclassifyAllArmed = false;
              btn.textContent = "重新分类全部";
            }, 3500);
            return;
          }
          clearTimeout(reclassifyAllTimer);
          reclassifyAllArmed = false;
          btn.textContent = "重新分类全部";
          btn.disabled = true;
          try {
            const data = await apiPost("reclassify", { all: true });
            toast("已加入队列：" + (data.queued || 0) + " 张，稍后刷新");
            setTimeout(load, 3500);
          } catch (error) {
            toast(error.message || String(error), true);
          } finally {
            btn.disabled = false;
          }
        });
        $("#safetyBtn").addEventListener("click", async () => {
          const next = state.settings.safety_filter_enabled === false;
          try {
            await apiPost("safety", { enabled: next });
            state.settings.safety_filter_enabled = next;
            renderSafety();
            toast("安全过滤已" + (next ? "开启" : "关闭"));
          } catch (error) {
            toast(error.message || String(error), true);
          }
        });
        $("#uploadBtn").addEventListener("click", () => $("#fileInput").click());
        $("#fileInput").addEventListener("change", async (event) => {
          const files = Array.from(event.target.files || []);
          event.target.value = "";
          if (!files.length) return;
          let ok = 0;
          for (const file of files) {
            try {
              await requireBridge().upload("upload", file);
              ok += 1;
            } catch (error) {
              toast(file.name + " 上传失败：" + (error.message || error), true);
            }
          }
          if (ok) {
            toast("已上传 " + ok + " 张，后台分类中");
            setTimeout(load, 1800);
          }
        });
        $("#grid").addEventListener("click", (event) => {
          const button = event.target.closest("button[data-action]");
          const card = event.target.closest(".card");
          if (!card) return;
          const item = state.items.find((x) => x.id === card.dataset.id);
          if (!item) return;
          if (!button) {
            if (event.target.closest("img")) openModal(item);
            return;
          }
          event.preventDefault();
          if (button.dataset.action === "edit") openModal(item);
          if (button.dataset.action === "reclassify") {
            apiPost("reclassify", { id: item.id })
              .then(() => { toast("已加入分类队列"); setTimeout(load, 2500); })
              .catch((error) => toast(error.message || String(error), true));
          }
          if (button.dataset.action === "toggle") toggleItem(item);
        });
        $("#saveBtn").addEventListener("click", saveModal);
        $("#deleteBtn").addEventListener("click", deleteCurrent);
        $("#retagBtn").addEventListener("click", retagCurrent);
        $("#reclassifyBtn").addEventListener("click", reclassifyCurrent);
        $("#tagPicker").addEventListener("click", (event) => {
          const tag = event.target.closest(".tag-choice");
          if (!tag) return;
          tag.classList.toggle("active");
        });
        $("#cancelBtn").addEventListener("click", closeModal);
        $("#closeModal").addEventListener("click", closeModal);
        $("#modal").addEventListener("click", (event) => {
          if (event.target === $("#modal")) closeModal();
        });
        $("#editIntensity").addEventListener("input", (event) => {
          $("#intensityValue").textContent = event.target.value;
        });
        document.addEventListener("keydown", (event) => {
          if (event.key === "Escape" && !$("#modal").hidden) closeModal();
        });
      }

      async function boot() {
        if (!B) {
          $("#loading").textContent = "没有检测到插件页面桥接，请从 AstrBot 插件详情页打开。";
          return;
        }
        try {
          const ctx = await B.ready();
          if (ctx && ctx.isDark) document.documentElement.dataset.theme = "dark";
          B.onContext((next) => {
            document.documentElement.dataset.theme = next.isDark ? "dark" : "light";
            document.title = (next.pageTitle || "表情包管理");
          });
          const title = B.t("pages.stickers.title", "表情包管理");
          $("#pageTitle").textContent = title;
          document.title = title;
        } catch (_) {
          // 独立打开时没有 context，也继续使用默认主题
        }
        bind();
        await load();
      }

      boot();
    })();

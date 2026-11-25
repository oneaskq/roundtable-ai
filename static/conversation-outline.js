/* Navigate between user turns in the scrollable part of the current chat. */
window.RoundtableOutline = {
  attach({panel, list, preview, scroller, content, messages, beforeJump}) {
    let turns = [], offsets = [], buttons = [], current = -1, previewIndex = -1;
    let frame = 0, needsLayout = true, tabIndex = 0, lastJump = null;

    function hidePreview() {
      preview.hidden = true;
      previewIndex = -1;
    }
    function showPreview(index) {
      if (index < 0 || !buttons[index] || panel.hidden) return hidePreview();
      previewIndex = index;
      const rect = buttons[index].getBoundingClientRect();
      const bounds = list.getBoundingClientRect();
      if (rect.bottom <= bounds.top || rect.top >= bounds.bottom) return hidePreview();
      preview.textContent = turns[index].label;
      preview.hidden = false;
      preview.style.top = (rect.top + rect.height / 2 - panel.getBoundingClientRect().top) + 'px';
    }
    function setTabStop(index) {
      if (buttons[tabIndex]) buttons[tabIndex].tabIndex = -1;
      tabIndex = index;
      if (buttons[index]) buttons[index].tabIndex = 0;
    }
    function mark(index) {
      if (index === current || !buttons[index]) return;
      buttons[current]?.removeAttribute('aria-current');
      current = index;
      buttons[index].setAttribute('aria-current', 'location');
      if (!list.contains(document.activeElement)) {
        setTabStop(index);
        const button = buttons[index];
        if (button.offsetTop < list.scrollTop) list.scrollTop = button.offsetTop;
        else if (button.offsetTop + button.offsetHeight > list.scrollTop + list.clientHeight)
          list.scrollTop = button.offsetTop + button.offsetHeight - list.clientHeight;
      }
    }
    function paint() {
      frame = 0;
      if (needsLayout) {
        needsLayout = false;
        const height = scroller.clientHeight;
        panel.hidden = turns.length < 2 || !height || scroller.scrollHeight - height < height / 2;
        if (panel.hidden) { hidePreview(); return; }
        panel.style.top = (scroller.offsetTop + height / 2) + 'px';
        list.style.maxHeight = Math.max(32, height - 48) + 'px';
        const origin = scroller.getBoundingClientRect().top - scroller.scrollTop;
        offsets = turns.map(turn => turn.node.getBoundingClientRect().top - origin);
      }
      if (panel.hidden) return;
      const top = scroller.scrollTop;
      if (lastJump && Math.abs(top - lastJump.position) <= 2) { mark(lastJump.index); return; }
      lastJump = null;
      if (top + scroller.clientHeight >= scroller.scrollHeight - 2) { mark(turns.length - 1); return; }
      const readingLine = top + Math.min(96, scroller.clientHeight * 0.22);
      let low = 0, high = offsets.length;
      while (low < high) {
        const mid = (low + high) >>> 1;
        if (offsets[mid] <= readingLine) low = mid + 1;
        else high = mid;
      }
      mark(Math.max(0, low - 1));
    }
    function schedule(layout = false) {
      needsLayout ||= layout;
      if (!frame) frame = requestAnimationFrame(paint);
    }
    function jump(index) {
      beforeJump();
      const delta = turns[index].node.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
      scroller.scrollTo({top: scroller.scrollTop + delta - 16, behavior: 'instant'});
      lastJump = {index, position: scroller.scrollTop};
      mark(index);
      hidePreview();
    }
    function makeButton(turn, index) {
      const button = document.createElement('button');
      button.type = 'button';
      button.tabIndex = -1;
      button.setAttribute('aria-controls', 'chat-turn-' + turn.id);
      button.setAttribute('aria-label', '跳到第 ' + (index + 1) + ' 次发言，' + turn.label);
      button.addEventListener('click', event => {
        jump(index);
        if (event.detail) scroller.focus({preventScroll: true});
      });
      button.addEventListener('pointerenter', event => { if (event.pointerType !== 'touch') showPreview(index); });
      button.addEventListener('pointerleave', hidePreview);
      button.addEventListener('focus', () => { setTabStop(index); showPreview(index); });
      button.addEventListener('blur', hidePreview);
      return button;
    }
    function refresh() {
      const next = Array.from(messages.querySelectorAll('article.message.user[data-message], article[data-user-turn]'), node => {
        const text = node.querySelector('.message-content, .team-chat-bubble')?.textContent || node.querySelector('textarea')?.value || '';
        const id = node.dataset.message || node.dataset.userTurn;
        node.id = 'chat-turn-' + id;
        const characters = Array.from(text.trim().replace(/\s+/g, ' '));
        return {node, id, label: characters.slice(0, 100).join('') + (characters.length > 100 ? '…' : '')};
      });
      const changed = next.length !== turns.length || next.some((turn, i) =>
        turn.id !== turns[i].id || turn.label !== turns[i].label);
      turns = next;
      if (changed) {
        const focusId = list.contains(document.activeElement) ? document.activeElement.dataset.turnId : null;
        hidePreview();
        lastJump = null;
        current = -1;
        buttons = turns.map((turn, index) => {
          const button = makeButton(turn, index);
          button.dataset.turnId = turn.id;
          return button;
        });
        list.replaceChildren(...buttons);
        setTabStop(0);
        if (focusId) buttons.find(button => button.dataset.turnId === focusId)?.focus({preventScroll: true});
      }
      schedule(true);
    }
    list.addEventListener('keydown', event => {
      if (event.key === 'Escape') { hidePreview(); return; }
      const next = {ArrowUp: tabIndex - 1, ArrowDown: tabIndex + 1, Home: 0, End: buttons.length - 1}[event.key];
      if (next === undefined) return;
      event.preventDefault();
      buttons[Math.max(0, Math.min(buttons.length - 1, next))]?.focus();
    });
    list.addEventListener('scroll', () => { if (previewIndex >= 0) showPreview(previewIndex); }, {passive: true});
    scroller.addEventListener('scroll', () => schedule(), {passive: true});
    content.addEventListener('toggle', () => schedule(true), true);
    const observer = new ResizeObserver(() => schedule(true));
    observer.observe(scroller);
    observer.observe(content);
    return {refresh};
  }
};

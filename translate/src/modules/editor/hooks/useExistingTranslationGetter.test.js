import { mount } from 'enzyme';
import React from 'react';
import sinon from 'sinon';

import { EditorData, EditorResult } from '~/context/Editor';
import * as Entity from '~/context/EntityView';
import { HistoryData } from '~/context/HistoryData';
import { editMessageEntry, parseEntry } from '~/utils/message';

import { useExistingTranslationGetter } from './useExistingTranslationGetter';

const ACTIVE_TRANSLATION = { pk: 1 };

const HISTORY_STRING = {
  translations: [
    { pk: 12, string: 'I was there before' },
    { pk: 98, string: 'hello, world!' },
    { pk: 10010, string: '' },
  ],
};

const HISTORY_FLUENT = {
  translations: [
    { pk: 12, string: 'msg = I like { -brand }' },
    { pk: 98, string: 'msg = hello, world!' },
    { pk: 431, string: 'msg = Come on Morty!\n' },
  ],
};

function mountSpy(format, history, editor) {
  const result = editor.fields.map(({ handle, keys, name }) => ({
    name,
    keys,
    value: handle.current.value,
  }));

  let res;
  const Spy = () => {
    res = useExistingTranslationGetter()();
    return null;
  };

  mount(
    <Entity.EntityView.Provider value={{ entity: { format } }}>
      <HistoryData.Provider value={history}>
        <EditorData.Provider value={editor}>
          <EditorResult.Provider value={result}>
            <Spy />
          </EditorResult.Provider>
        </EditorData.Provider>
      </HistoryData.Provider>
    </Entity.EntityView.Provider>,
  );

  return res;
}

const mockMessageEntry = (value) => ({
  id: 'msg',
  value: { type: 'message', declarations: [], pattern: [value] },
});

const mockEditorMessage = (value) => [
  { id: 'msg', name: '', keys: [], labels: [], handle: { current: { value } } },
];

describe('useExistingTranslation', () => {
  beforeAll(() =>
    sinon.stub(Entity, 'useActiveTranslation').returns(ACTIVE_TRANSLATION),
  );
  afterAll(() => Entity.useActiveTranslation.restore());

  it('finds identical initial/active translation', () => {
    const entry = mockMessageEntry('something');
    const res = mountSpy('simple', HISTORY_STRING, {
      entry,
      fields: editMessageEntry(entry),
      initial: entry,
    });

    expect(res).toBe(ACTIVE_TRANSLATION);
  });

  it('finds identical Fluent initial/active translation', () => {
    const entry = parseEntry('ftl', 'msg = something');
    const res = mountSpy('ftl', HISTORY_FLUENT, {
      entry,
      fields: editMessageEntry(entry),
      initial: entry,
    });

    expect(res).toBe(ACTIVE_TRANSLATION);
  });

  it('finds empty initial/active translation', () => {
    const entry = mockMessageEntry('');
    const res = mountSpy('simple', HISTORY_STRING, {
      entry,
      fields: editMessageEntry(entry),
      initial: entry,
    });

    expect(res).toBe(ACTIVE_TRANSLATION);
  });

  it('finds identical translation in history', () => {
    const entry = mockMessageEntry('');
    const prev0 = HISTORY_STRING.translations[0];
    const res0 = mountSpy('simple', HISTORY_STRING, {
      entry,
      fields: mockEditorMessage(prev0.string),
      initial: entry,
    });

    expect(res0).toBe(prev0);

    const prev1 = HISTORY_STRING.translations[1];
    const res1 = mountSpy('simple', HISTORY_STRING, {
      entry,
      fields: mockEditorMessage(prev1.string),
      initial: entry,
    });

    expect(res1).toBe(prev1);
  });

  it('finds identical Fluent translation in history', () => {
    const entry = parseEntry('ftl', 'msg = something');
    const prev0 = HISTORY_FLUENT.translations[0];
    const res0 = mountSpy('ftl', HISTORY_FLUENT, {
      entry,
      fields: editMessageEntry(parseEntry('ftl', prev0.string)),
      initial: entry,
    });

    expect(res0).toBe(prev0);

    const prev1 = HISTORY_FLUENT.translations[1];
    const res1 = mountSpy('ftl', HISTORY_FLUENT, {
      entry,
      fields: editMessageEntry(parseEntry('ftl', prev1.string)),
      initial: entry,
    });

    expect(res1).toBe(prev1);
  });

  it('finds empty translation in history', () => {
    const entry = mockMessageEntry('x');
    const res = mountSpy('simple', HISTORY_STRING, {
      entry,
      fields: mockEditorMessage(''),
      initial: entry,
    });

    expect(res).toBe(HISTORY_STRING.translations[2]);
  });

  it('finds a Fluent translation in history', () => {
    const entry = parseEntry('ftl', 'msg = something');
    const res = mountSpy('ftl', HISTORY_FLUENT, {
      entry,
      fields: mockEditorMessage('Come on Morty!'),
      initial: entry,
    });

    expect(res).toBe(HISTORY_FLUENT.translations[2]);
  });
});

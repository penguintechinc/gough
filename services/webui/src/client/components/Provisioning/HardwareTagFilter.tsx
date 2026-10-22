/**
 * Hardware Tag Filter Component
 *
 * Autocompleting filter for hardware tags.
 */

import React, { useState, useRef, useEffect } from 'react';

interface HardwareTagFilterProps {
  selectedTags: string[];
  onTagsChange: (tags: string[]) => void;
  vocabulary: string[];
}

export const HardwareTagFilter: React.FC<HardwareTagFilterProps> = ({
  selectedTags,
  onTagsChange,
  vocabulary,
}) => {
  const [inputValue, setInputValue] = useState('');
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (inputValue.length > 0) {
      const filtered = vocabulary.filter(
        (tag) =>
          tag.toLowerCase().includes(inputValue.toLowerCase()) &&
          !selectedTags.includes(tag)
      );
      setSuggestions(filtered.slice(0, 5));
      setShowSuggestions(true);
    } else {
      setSuggestions([]);
      setShowSuggestions(false);
    }
  }, [inputValue, selectedTags, vocabulary]);

  const handleAddTag = (tag: string) => {
    if (!selectedTags.includes(tag)) {
      onTagsChange([...selectedTags, tag]);
    }
    setInputValue('');
    setShowSuggestions(false);
  };

  const handleRemoveTag = (tag: string) => {
    onTagsChange(selectedTags.filter((t) => t !== tag));
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2 mb-3">
        {selectedTags.map((tag) => (
          <div
            key={tag}
            className="inline-flex items-center gap-1 px-2 py-1 bg-gold-900/30 border border-gold-700 text-gold-300 rounded text-sm"
          >
            {tag}
            <button
              onClick={() => handleRemoveTag(tag)}
              className="text-gold-400 hover:text-gold-200 ml-1"
            >
              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        ))}
      </div>

      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          placeholder="Add hardware tags..."
          className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded text-white placeholder-dark-500 focus:border-gold-600 focus:outline-none"
        />

        {showSuggestions && suggestions.length > 0 && (
          <div className="absolute top-full left-0 right-0 mt-1 bg-dark-900 border border-dark-700 rounded shadow-lg z-10">
            {suggestions.map((tag) => (
              <button
                key={tag}
                onClick={() => handleAddTag(tag)}
                className="w-full text-left px-3 py-2 hover:bg-dark-800 text-sm text-dark-200 first:rounded-t last:rounded-b transition-colors"
              >
                {tag}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

export default HardwareTagFilter;

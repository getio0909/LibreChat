import { app } from '../../../scripts/app.js';

app.registerExtension({
  name: 'zyb.design.referenceCategories',
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== 'ZybObsidianReference') return;
    const [allValues, config] = nodeData.input.required['参考资料'];
    const categories = config.zyb_categories;
    const originalCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function (...args) {
      const result = originalCreated?.apply(this, args);
      const category = this.widgets.find(w => w.name === '资料类别');
      const reference = this.widgets.find(w => w.name === '参考资料');
      const choices = () => category.value === '全部资料'
        ? allValues : (categories[category.value] || []).map(i => allValues[i]);
      reference.options.values = choices;
      const originalCallback = category.callback;
      category.callback = function (...values) {
        originalCallback?.apply(this, values);
        const available = choices();
        if (!available.includes(reference.value) && available.length) {
          reference.value = available[0];
          reference.callback?.(reference.value);
        }
        app.graph.setDirtyCanvas(true, true);
      };
      return result;
    };
  }
});
